"""
Booking API — settings shared by every environment.

Environment files (local / staging / production) extend this one and list
only what differs. Secrets come from the environment and have no defaults
here, so a deployment that forgets one refuses to start instead of running
with a guessable value.
"""

from pathlib import Path

import dj_database_url
from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = config('SECRET_KEY')
DEBUG = False
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1', cast=Csv())
ENVIRONMENT_NAME = config('ENVIRONMENT_NAME', default='production')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.postgres',
    'rest_framework',
    'drf_spectacular',
    'booking',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [],
    'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages',
    ]},
}]

# Postgres only: the no-double-booking guarantee is an exclusion constraint
# over time ranges, which no other database has.
DATABASES = {'default': dj_database_url.parse(config('DATABASE_URL'), conn_max_age=60,
                                              conn_health_checks=True)}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 12}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-gb'
# Only used for the admin and logs. Every resource carries its own time zone,
# and every instant is stored in UTC.
TIME_ZONE = 'Africa/Accra'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}

# The Django admin lives at a private path in deployed environments.
ADMIN_URL = config('ADMIN_URL', default='admin/')

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
# Platforms authenticate with an API key. Nothing is public except /health/.
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': ['booking.auth.PlatformKeyAuthentication'],
    'DEFAULT_PERMISSION_CLASSES': ['booking.auth.IsPlatform'],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 100,
    'TEST_REQUEST_DEFAULT_FORMAT': 'json',
    'EXCEPTION_HANDLER': 'booking.errors.exception_handler',
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Booking API',
    'DESCRIPTION': 'Time for any platform: resources, opening hours, availability, holds and '
                   'bookings, with a database-level guarantee that nothing is double-booked. '
                   'Each platform sees only its own resources and bookings.',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
}

# Fernet keys that encrypt each platform's webhook signing secret. The FIRST
# key encrypts; every key decrypts (rotation: new key first, re-save, drop old).
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
BOOKING_SECRET_KEYS = config('BOOKING_SECRET_KEYS', cast=Csv())

# Hold limits, in minutes. A platform picks the length per hold within these.
HOLD_DEFAULT_MINUTES = config('HOLD_DEFAULT_MINUTES', default=15, cast=int)
HOLD_MAX_MINUTES = config('HOLD_MAX_MINUTES', default=7 * 24 * 60, cast=int)
# Widest window one availability search may cover.
AVAILABILITY_MAX_DAYS = config('AVAILABILITY_MAX_DAYS', default=62, cast=int)

# ---------------------------------------------------------------------------
# Background work — hold expiry and webhook delivery
# ---------------------------------------------------------------------------
# Without a broker, webhooks are sent inline after commit and overdue holds
# are expired when someone else asks for their time; run the worker anyway.
CELERY_BROKER_URL = config('REDIS_URL', default='')
CELERY_RESULT_BACKEND = None
CELERY_TASK_IGNORE_RESULT = True
CELERY_TIMEZONE = 'UTC'
CELERY_BEAT_SCHEDULE = {
    'expire-holds': {'task': 'booking.tasks.expire_holds', 'schedule': 60.0},
    'retry-webhooks': {'task': 'booking.tasks.retry_webhooks', 'schedule': 60.0},
    'purge-idempotency-keys': {'task': 'booking.tasks.purge_idempotency_keys', 'schedule': 6 * 3600.0},
}

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {'plain': {'format': '%(asctime)s %(levelname)s %(name)s %(message)s'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'formatter': 'plain'}},
    'root': {'handlers': ['console'], 'level': config('LOG_LEVEL', default='INFO')},
}
