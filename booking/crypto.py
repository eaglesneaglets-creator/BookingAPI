"""Encrypts each platform's webhook signing secret at rest (Fernet, rotatable)."""

from functools import lru_cache

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings


@lru_cache(maxsize=1)
def _fernet() -> MultiFernet:
    keys = [k.strip() for k in settings.BOOKING_SECRET_KEYS if k.strip()]
    return MultiFernet([Fernet(k.encode()) for k in keys])


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode() if token else ''
