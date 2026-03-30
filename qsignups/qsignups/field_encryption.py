"""Field encryption using Fernet (AES-128-CBC + HMAC-SHA256).

``DB_ENCRYPTION_KEY`` is stretched to a 32-byte key using PBKDF2-HMAC-SHA256
with 600,000 iterations. When unset or placeholder, values pass through unchanged.
"""

from __future__ import annotations

import base64
import functools
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

_logger = logging.getLogger(__name__)

_ENV_KEY = "DB_ENCRYPTION_KEY"
_PLACEHOLDER_VALUES = frozenset({"", "123"})
_PBKDF2_ITERATIONS = 600_000
_PBKDF2_SALT_PREFIX = b"slack-stack-db-fernet-v1"
_FERNET_PREFIX = b"gAAAAA"


@functools.lru_cache(maxsize=2)
def _get_fernet(passphrase: str) -> Fernet:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt = _PBKDF2_SALT_PREFIX + passphrase.encode()[:16]
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    derived = kdf.derive(passphrase.encode())
    return Fernet(base64.urlsafe_b64encode(derived))


def _encryption_enabled() -> bool:
    key = os.environ.get(_ENV_KEY, "").strip()
    return bool(key) and key not in _PLACEHOLDER_VALUES


def encrypt_field(value: Optional[str]) -> Optional[str]:
    """Encrypt a string for DB storage. Returns *value* unchanged if encryption off."""
    if value is None or value == "":
        return value
    if not _encryption_enabled():
        return value
    key = os.environ[_ENV_KEY].strip()
    return _get_fernet(key).encrypt(value.encode()).decode()


def decrypt_field(encrypted: Optional[str]) -> Optional[str]:
    """Decrypt a string from DB. Plaintext legacy values are returned as-is."""
    if encrypted is None or encrypted == "":
        return encrypted
    if not _encryption_enabled():
        return encrypted
    raw = encrypted.encode()
    if not raw.startswith(_FERNET_PREFIX):
        return encrypted
    key = os.environ[_ENV_KEY].strip()
    try:
        return _get_fernet(key).decrypt(raw).decode()
    except InvalidToken:
        _logger.warning("decrypt_field: InvalidToken — returning raw value")
        return encrypted
