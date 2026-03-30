"""Field encryption using Fernet (AES-128-CBC + HMAC-SHA256).

``DB_ENCRYPTION_KEY`` is stretched to a 32-byte key using PBKDF2-HMAC-SHA256
with 600,000 iterations. The key is required at runtime; call
``require_encryption_key()`` at process startup to fail fast.
"""

from __future__ import annotations

import base64
import functools
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet

_LOG = logging.getLogger(__name__)
_ENV_KEY = "DB_ENCRYPTION_KEY"
_PLACEHOLDER_VALUES = frozenset({"", "123"})
_MIN_KEY_LENGTH = 16
_PBKDF2_ITERATIONS = 600_000
_PBKDF2_SALT_PREFIX = b"slack-stack-db-fernet-v1"
_FERNET_PREFIX = b"gAAAAA"


def require_encryption_key() -> str:
    """Return validated ``DB_ENCRYPTION_KEY`` or raise ``RuntimeError``."""
    key = os.environ.get(_ENV_KEY, "").strip()
    if not key or key in _PLACEHOLDER_VALUES:
        raise RuntimeError(
            f"{_ENV_KEY} is required. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    if len(key) < _MIN_KEY_LENGTH:
        raise RuntimeError(
            f"{_ENV_KEY} must be at least {_MIN_KEY_LENGTH} characters (got {len(key)})"
        )
    return key


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


def encrypt_field(value: Optional[str]) -> Optional[str]:
    """Encrypt a string for DB storage."""
    if value is None or value == "":
        return value
    key = require_encryption_key()
    return _get_fernet(key).encrypt(value.encode()).decode()


def decrypt_field(encrypted: Optional[str]) -> Optional[str]:
    """Decrypt a Fernet-encrypted string from DB.

    Non-empty values must be valid Fernet ciphertext (plaintext tokens are not supported).
    """
    if encrypted is None or encrypted == "":
        return encrypted
    key = require_encryption_key()
    raw = encrypted.encode()
    if not raw.startswith(_FERNET_PREFIX):
        _LOG.warning("decrypt_field: value is not Fernet ciphertext (len=%s)", len(encrypted or ""))
        raise ValueError(
            "decrypt_field: value is not Fernet-encrypted (plaintext tokens are not supported)"
        )
    return _get_fernet(key).decrypt(raw).decode()
