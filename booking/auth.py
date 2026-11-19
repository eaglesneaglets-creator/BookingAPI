"""
Who is calling: a platform, identified by its API key.

Platforms send   Authorization: Bearer bk_<prefix>_<secret>

The key decides the platform, and the platform decides everything the call
can see or change. There is no way to name another platform in a request.
"""

import hmac
from datetime import timedelta

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission

from .models import ApiKey, sha256_hex


class PlatformPrincipal:
    """Stands in for request.user when a platform calls."""

    is_authenticated = True
    is_staff = False

    def __init__(self, key: ApiKey):
        self.key = key
        self.platform = key.platform

    def __str__(self):
        return f'platform:{self.platform.slug}'


class PlatformKeyAuthentication(BaseAuthentication):
    def authenticate(self, request):
        header = get_authorization_header(request).decode('latin-1')
        if not header.startswith('Bearer '):
            return None
        parts = header[len('Bearer '):].strip().split('_', 2)
        if len(parts) != 3 or parts[0] != 'bk':
            raise AuthenticationFailed('Invalid API key.')
        key = (ApiKey.objects.select_related('platform')
               .filter(prefix=parts[1], revoked_at__isnull=True, platform__is_active=True).first())
        # Same failure for an unknown prefix and a wrong secret, compared in
        # constant time, so neither can be probed.
        expected = key.secret_hash if key else '0' * 64
        if not hmac.compare_digest(expected, sha256_hex(parts[2])) or key is None:
            raise AuthenticationFailed('Invalid API key.')
        now = timezone.now()
        if key.last_used_at is None or now - key.last_used_at > timedelta(minutes=5):
            ApiKey.objects.filter(pk=key.pk).update(last_used_at=now)
        return PlatformPrincipal(key), key

    def authenticate_header(self, request):
        return 'Bearer'


class IsPlatform(BasePermission):
    message = 'This endpoint takes a platform API key.'

    def has_permission(self, request, view):
        return isinstance(request.user, PlatformPrincipal)
