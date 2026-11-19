"""
Local development and tests.

Fills in throwaway values for every secret so `manage.py` and `pytest` run
with no setup. These values are public — they are in this file — so nothing
encrypted with them is protected. Deployed settings never import this file.

Needs Postgres (exclusion constraints): `docker compose up -d db` starts one
on port 5434 with the URL below.
"""

import os

os.environ.setdefault('SECRET_KEY', 'local-dev-only-not-secret')
os.environ.setdefault('DATABASE_URL', 'postgres://booking:booking@127.0.0.1:5434/booking')
os.environ.setdefault('ENVIRONMENT_NAME', 'local')
# 32 zero bytes, base64 — a valid Fernet key that protects nothing.
os.environ.setdefault('BOOKING_SECRET_KEYS', 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=')

from .base import *  # noqa: E402

DEBUG = True
ALLOWED_HOSTS = ['*']
