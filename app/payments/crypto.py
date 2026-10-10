"""Encryption for secrets stored in the database (organisations' own
Paystack secret keys).

Uses Fernet (AES-128-CBC + HMAC). The key comes from FIELD_ENCRYPTION_KEY
if set (generate one with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
), otherwise it is derived from SECRET_KEY so a deployment works with no
extra setup. The catch with the fallback: rotating SECRET_KEY makes stored
keys undecryptable -- which is handled gracefully (the organisation is
asked to re-enter its Paystack key), never as a crash. Set a dedicated
FIELD_ENCRYPTION_KEY to avoid that.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


class DecryptError(Exception):
    pass


def _fernet():
    configured = os.environ.get("FIELD_ENCRYPTION_KEY")
    if configured:
        return Fernet(configured.encode())
    secret = current_app.config["SECRET_KEY"].encode()
    derived = hashlib.sha256(b"acquiral-field-encryption-v1|" + secret).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(plaintext):
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token):
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        raise DecryptError("Stored secret can't be decrypted with the current key.")
