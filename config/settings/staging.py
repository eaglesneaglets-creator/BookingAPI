"""Booking API — staging. Production in every respect except its name."""

from .production import *  # noqa: F403

ENVIRONMENT_NAME = 'staging'
