"""Application-level encryption for PII/PHI fields.

Uses Fernet symmetric encryption. The key is loaded from an environment variable
now and will map to AWS KMS in Phase 4. All PII/PHI fields are encrypted at rest
at the application layer before being written to the database.

EncryptedString is a SQLAlchemy TypeDecorator that automatically encrypts on write
and decrypts on read — any column using this type has encryption enforced at the
ORM layer with zero caller effort.
"""

import base64
import hashlib

from cryptography.fernet import Fernet
from sqlalchemy import String, Text
from sqlalchemy.types import TypeDecorator

from app.config import settings


def _get_fernet() -> Fernet:
    key = settings.encryption_key
    if not key:
        raise RuntimeError("ENCRYPTION_KEY is not set")
    # Derive a valid 32-byte Fernet key from the provided secret
    derived = hashlib.sha256(key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_value(plaintext: str) -> str:
    if not plaintext:
        return plaintext
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    if not ciphertext:
        return ciphertext
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()


import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class AES256GCMEncryptor:
    """AES-256-GCM encryption — HIPAA best-practice standard.

    Provides authenticated encryption with 12-byte nonce. The nonce is
    prepended to the ciphertext and stripped on decryption.
    """

    def __init__(self, key: str):
        derived = hashlib.sha256(key.encode()).digest()  # 32 bytes = AES-256
        self._gcm = AESGCM(derived)

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(12)
        ct = self._gcm.encrypt(nonce, plaintext.encode(), None)
        return base64.urlsafe_b64encode(nonce + ct).decode()

    def decrypt(self, ciphertext: str) -> str:
        data = base64.urlsafe_b64decode(ciphertext.encode())
        nonce, ct = data[:12], data[12:]
        return self._gcm.decrypt(nonce, ct, None).decode()


def get_aes256_encryptor() -> AES256GCMEncryptor:
    """Return an AES-256-GCM encryptor initialized from the app encryption key."""
    key = settings.encryption_key
    if not key:
        raise RuntimeError("ENCRYPTION_KEY is not set")
    return AES256GCMEncryptor(key)


class EncryptedString(TypeDecorator):
    """Column type that transparently encrypts on write and decrypts on read.

    Usage:
        ein_encrypted = mapped_column(EncryptedString(), nullable=True)
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or value == "":
            return value
        return encrypt_value(value)

    def process_result_value(self, value, dialect):
        if value is None or value == "":
            return value
        return decrypt_value(value)
