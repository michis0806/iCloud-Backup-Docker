"""Fernet-based encryption for stored iCloud account passwords.

The encryption key is derived (SHA-256) from ICLOUD_SECRET_KEY, falling
back to SECRET_KEY / AUTH_PASSWORD when those are explicitly configured.
Randomly generated per-start secrets are never used, because ciphertexts
must stay decryptable across container restarts.
"""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

log = logging.getLogger("icloud-backup")


def _fernet() -> Fernet | None:
    key_source = settings.get_icloud_secret_key()
    if not key_source:
        return None
    digest = hashlib.sha256(key_source.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encryption_available() -> bool:
    """Return True when a stable encryption key is configured."""
    return _fernet() is not None


def encrypt(plaintext: str) -> str | None:
    """Encrypt *plaintext*, returning a token string or None if no key is set."""
    fernet = _fernet()
    if fernet is None:
        return None
    return fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str | None:
    """Decrypt *token*, returning the plaintext or None.

    Returns None when no key is configured or the token was encrypted with
    a different key (e.g. ICLOUD_SECRET_KEY changed since it was stored).
    """
    fernet = _fernet()
    if fernet is None:
        return None
    try:
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        log.warning(
            "Gespeichertes iCloud-Passwort konnte nicht entschlüsselt werden "
            "(ICLOUD_SECRET_KEY geändert?). Bitte Passwort neu speichern."
        )
        return None
