"""Fernet token round-trip — guards a cryptography major bump."""

from __future__ import annotations

import os

os.environ.setdefault("DB_ENCRYPTION_KEY", "test-encryption-key-32chars!!")

from common.encryption import decrypt_field, encrypt_field, require_encryption_key


def test_encrypt_decrypt_round_trip_preserves_slack_token():
    require_encryption_key()
    token = "xoxb-rehearsal-valid-looking-token"
    encrypted = encrypt_field(token)
    assert encrypted != token
    assert encrypted.startswith("gAAAAA")
    assert decrypt_field(encrypted) == token


def test_decrypt_rejects_plaintext_token():
    try:
        decrypt_field("xoxb-plaintext")
    except ValueError as exc:
        assert "not Fernet-encrypted" in str(exc)
    else:
        raise AssertionError("expected ValueError for plaintext token")
