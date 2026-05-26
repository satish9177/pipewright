"""
secrets.py
Encrypts and decrypts stored project secrets.
"""

import os

from cryptography.fernet import Fernet, InvalidToken

from backend.config.keys import settings

ENCRYPTED_PREFIX = "fernet:"
ENCRYPTION_KEY_ENV = "PIPEWRIGHT_ENCRYPTION_KEY"


def _get_encryption_key() -> str | None:
    return settings.pipewright_encryption_key or os.getenv(ENCRYPTION_KEY_ENV)


def _get_fernet() -> Fernet:
    key = _get_encryption_key()
    if not key:
        raise RuntimeError(
            f"secrets.py: {ENCRYPTION_KEY_ENV} is required for GitHub token storage"
        )
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as error:
        raise RuntimeError(
            f"secrets.py: invalid {ENCRYPTION_KEY_ENV}: {error}"
        )


def encrypt_secret(value: str | None) -> str | None:
    if value is None or value == "":
        return value
    if value.startswith(ENCRYPTED_PREFIX):
        return value
    token = _get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTED_PREFIX}{token}"


def decrypt_secret(value: str | None) -> str | None:
    if value is None or value == "":
        return value
    if not value.startswith(ENCRYPTED_PREFIX):
        raise RuntimeError(
            "secrets.py: unencrypted GitHub token found. "
            "Re-save the project token to encrypt it before GitHub operations."
        )
    encrypted = value[len(ENCRYPTED_PREFIX):]
    try:
        return _get_fernet().decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except InvalidToken as error:
        raise RuntimeError("secrets.py: failed to decrypt GitHub token") from error
