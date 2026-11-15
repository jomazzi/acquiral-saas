"""Encrypts the 2FA secret at rest with the same FIELD_ENCRYPTION_KEY used
for stored payment-provider keys."""
import os

from cryptography.fernet import Fernet


def _fernet():
    key = os.environ.get("FIELD_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("FIELD_ENCRYPTION_KEY must be set for the ops console.")
    return Fernet(key.encode())


def enc(text):
    return _fernet().encrypt(text.encode()).decode()


def dec(token):
    return _fernet().decrypt(token.encode()).decode()