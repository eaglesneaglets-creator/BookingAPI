"""Booking API — production."""

from decouple import Csv, config
from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403

DEBUG = False

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = True
# Railway's health check calls over plain HTTP inside the network.
SECURE_REDIRECT_EXEMPT = [r'^health/$']
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_AGE = 8 * 3600
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'
CSRF_TRUSTED_ORIGINS = config('CSRF_TRUSTED_ORIGINS', default='', cast=Csv())

if ADMIN_URL.strip('/') == 'admin':  # noqa: F405
    raise ImproperlyConfigured('ADMIN_URL is the default "admin/". Set a private path.')
if not DATABASES['default']['ENGINE'].endswith('postgresql'):  # noqa: F405
    raise ImproperlyConfigured('DATABASE_URL must be Postgres: double-booking is prevented by a Postgres constraint.')
