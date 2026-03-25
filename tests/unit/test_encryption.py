"""Tests for PII/PHI encryption."""

import os


def test_encrypt_decrypt():
    os.environ["ENCRYPTION_KEY"] = "test-key-for-encryption"
    # Re-import to pick up the new env var
    from app.encryption import encrypt_value, decrypt_value

    original = "123-45-6789"
    encrypted = encrypt_value(original)

    assert encrypted != original
    assert decrypt_value(encrypted) == original


def test_encrypt_empty():
    os.environ["ENCRYPTION_KEY"] = "test-key-for-encryption"
    from app.encryption import encrypt_value

    assert encrypt_value("") == ""
    assert encrypt_value(None) is None
