"""
Booking API — data model.

    Platform ─┬─ ApiKey
              ├─ Resource ─┬─ WeeklyHours       opening hours, local wall-clock time
              │            └─ DateOverride      one date's hours replaced (or closed)
              ├─ Closure                        platform-wide shut periods (holidays)
              ├─ Block                          one resource unavailable (maintenance, time off)
              └─ Booking                        held → confirmed / cancelled / expired
                                   Allocation   resource × period, for a Booking or a Block

THE GUARANTEE. Every Allocation that is `active` takes its resource for its
period, and one exclusion constraint makes Postgres refuse a second active
Allocation of the same resource whose period overlaps. That holds under any
amount of concurrency, from any code path, because the database enforces it,
not the application. A booking that needs a room and an engineer writes two
Allocations in one transaction: both are free or neither is taken.

ISOLATION. Every row belongs to one platform, every query is filtered by the
calling platform, and a resource belongs to exactly one platform, so one
platform's bookings can never block, reveal or touch another's. The same
external_ref ("room-a") on two platforms is two unrelated resources.

Periods are half-open [start, end): a booking ending at 12:00 and one starting
at 12:00 do not clash. Every instant is stored in UTC.
"""

import hashlib
import secrets
import uuid
from zoneinfo import available_timezones

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import ArrayField, DateTimeRangeField, RangeOperators
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def validate_timezone(value: str) -> None:
    if value not in available_timezones():
        raise ValidationError(f'"{value}" is not an IANA time zone (e.g. "Africa/Accra").')


# ---------------------------------------------------------------------------
# Platforms and their keys
# ---------------------------------------------------------------------------

class Platform(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(unique=True, help_text='Short stable name, e.g. "ona-records".')
    name = models.CharField(max_length=120)
    webhook_url = models.URLField(blank=True, help_text='Where booking events are POSTed. Blank: no webhooks.')
    # Encrypted, not hashed: it must be readable to sign with.
    webhook_secret_encrypted = models.TextField(blank=True, editable=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class ApiKey(models.Model):
    """
    A platform's credential, shown once as bk_<prefix>_<secret>. Only the
    prefix (to find the row) and a SHA-256 of the secret are stored; the
    secret is 256 random bits, so a plain hash leaves nothing to brute-force.
    """

    platform = models.ForeignKey(Platform, on_delete=models.CASCADE, related_name='api_keys')
    name = models.CharField(max_length=80, help_text='Where this key is used, e.g. "ONA staging backend".')
    prefix = models.CharField(max_length=16, unique=True, editable=False)
    secret_hash = models.CharField(max_length=64, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'{self.platform.slug}: {self.name} (bk_{self.prefix}_…)'

    @classmethod
    def issue(cls, platform: Platform, name: str) -> tuple['ApiKey', str]:
        """Create a key and return it with the raw value — the only time it exists."""
        prefix = secrets.token_hex(4)
        secret = secrets.token_urlsafe(32)
        key = cls.objects.create(platform=platform, name=name, prefix=prefix, secret_hash=sha256_hex(secret))
        return key, f'bk_{prefix}_{secret}'


# ---------------------------------------------------------------------------
# Resources and their opening hours
# ---------------------------------------------------------------------------

class Resource(models.Model):
    """Anything that can be booked for a period: a room, an engineer, a mentor, a microphone."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    platform = models.ForeignKey(Platform, on_delete=models.CASCADE, related_name='resources')
    external_ref = models.CharField(
        max_length=120, help_text="The platform's own id for this resource (e.g. ONA's Room UUID).")
    kind = models.SlugField(max_length=40, help_text='Chosen by the platform: "room", "engineer", "mentor"…')
    name = models.CharField(max_length=160)
    timezone = models.CharField(max_length=64, default='Africa/Accra', validators=[validate_timezone],
                                help_text='Opening hours are read in this zone.')
    is_active = models.BooleanField(default=True, help_text='Inactive: never offered, cannot be held.')
    # Kept free around every booking of this resource (set-up, cleaning,
    # travel). Part of the allocated period, so they really are blocked.
    buffer_before_minutes = models.PositiveIntegerField(default=0, validators=[MaxValueValidator(24 * 60)])
    buffer_after_minutes = models.PositiveIntegerField(default=0, validators=[MaxValueValidator(24 * 60)])
    # Booking window: at least this long before the start, at most this far ahead.
    min_notice_minutes = models.PositiveIntegerField(default=0, validators=[MaxValueValidator(365 * 24 * 60)])
    max_advance_days = models.PositiveIntegerField(default=365, validators=[MinValueValidator(1),
                                                                            MaxValueValidator(5 * 365)])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['kind', 'name']
        constraints = [
            models.UniqueConstraint(fields=['platform', 'external_ref'], name='resource_ref_unique_per_platform'),
        ]
        indexes = [models.Index(fields=['platform', 'kind'])]

    def __str__(self):
        return f'{self.name} ({self.kind})'


class WeeklyHours(models.Model):
    """Open from start_minute to end_minute (minutes after local midnight) every `weekday`."""

    resource = models.ForeignKey(Resource, on_delete=models.CASCADE, related_name='weekly_hours')
    weekday = models.PositiveSmallIntegerField(validators=[MaxValueValidator(6)], help_text='0 = Monday … 6 = Sunday')
    start_minute = models.PositiveSmallIntegerField(validators=[MaxValueValidator(1439)])
    end_minute = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(1440)],
                                                  help_text='1440 = midnight at the end of the day')

    class Meta:
        ordering = ['weekday', 'start_minute']
        constraints = [
            models.CheckConstraint(condition=models.Q(end_minute__gt=models.F('start_minute')),
                                   name='weekly_hours_end_after_start'),
        ]


class DateOverride(models.Model):
    """One local date's hours, replacing the weekly ones. No hours: closed that day."""

    resource = models.ForeignKey(Resource, on_delete=models.CASCADE, related_name='overrides')
    date = models.DateField()
    # [[start_minute, end_minute], …]
    hours = models.JSONField(default=list, blank=True)
    reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['date']
        constraints = [models.UniqueConstraint(fields=['resource', 'date'], name='one_override_per_resource_date')]


class Closure(models.Model):
    """The platform shut for a period (a public holiday): every resource, or only some kinds."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    platform = models.ForeignKey(Platform, on_delete=models.CASCADE, related_name='closures')
    period = DateTimeRangeField()
    kinds = ArrayField(models.SlugField(max_length=40), default=list, blank=True,
                       help_text='Empty: every kind of resource.')
    reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['period']


# ---------------------------------------------------------------------------
# Bookings, blocks and the allocations that take time
# ---------------------------------------------------------------------------

class BookingStatus(models.TextChoices):
    HELD = 'held', 'Held'                 # time taken until hold_expires_at, awaiting confirmation
    CONFIRMED = 'confirmed', 'Confirmed'  # time taken
    CANCELLED = 'cancelled', 'Cancelled'  # released by the platform
    EXPIRED = 'expired', 'Expired'        # released because the hold ran out


class Booking(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    platform = models.ForeignKey(Platform, on_delete=models.CASCADE, related_name='bookings')
    subject_ref = models.CharField(max_length=120, blank=True, help_text="The platform's id for who booked.")
    external_ref = models.CharField(max_length=120, blank=True, help_text="The platform's id for this booking.")
    status = models.CharField(max_length=10, choices=BookingStatus.choices, default=BookingStatus.HELD)
    # The booked time itself; Allocation periods add each resource's buffers.
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    hold_expires_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=200, blank=True)
    metadata = models.JSONField(default=dict, blank=True, help_text='Anything the platform wants back. Not read.')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['start_at']
        constraints = [
            models.CheckConstraint(condition=models.Q(end_at__gt=models.F('start_at')),
                                   name='booking_ends_after_start'),
            models.CheckConstraint(
                condition=~models.Q(status='held') | models.Q(hold_expires_at__isnull=False),
                name='held_booking_has_expiry'),
            models.UniqueConstraint(fields=['platform', 'external_ref'], condition=~models.Q(external_ref=''),
                                    name='booking_ref_unique_per_platform'),
        ]
        indexes = [
            models.Index(fields=['platform', 'start_at']),
            models.Index(fields=['status', 'hold_expires_at'], name='booking_hold_sweep'),
        ]

    def __str__(self):
        return f'{self.platform.slug} · {self.start_at:%Y-%m-%d %H:%M} · {self.status}'


class Block(models.Model):
    """One resource unavailable for a period: maintenance, time off, a private session."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    platform = models.ForeignKey(Platform, on_delete=models.CASCADE, related_name='blocks')
    resource = models.ForeignKey(Resource, on_delete=models.CASCADE, related_name='blocks')
    period = DateTimeRangeField()
    kind = models.SlugField(max_length=40, default='maintenance')
    reason = models.CharField(max_length=200, blank=True)
    external_ref = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['period']


class Allocation(models.Model):
    """One resource taken for one period, by a booking or a block. See THE GUARANTEE above."""

    platform = models.ForeignKey(Platform, on_delete=models.CASCADE, related_name='+')
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name='allocations')
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, null=True, blank=True, related_name='allocations')
    block = models.OneToOneField(Block, on_delete=models.CASCADE, null=True, blank=True, related_name='allocation')
    # Which requirement group of the booking this fills (0, 1, …).
    group = models.PositiveSmallIntegerField(default=0)
    period = DateTimeRangeField()
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(booking__isnull=False, block__isnull=True)
                           | models.Q(booking__isnull=True, block__isnull=False)),
                name='allocation_is_booking_or_block'),
            ExclusionConstraint(
                name='no_double_booking',
                expressions=[('resource', RangeOperators.EQUAL), ('period', RangeOperators.OVERLAPS)],
                condition=models.Q(active=True),
                violation_error_message='That resource is already taken for part of this time.',
            ),
        ]
        indexes = [models.Index(fields=['booking'])]


# ---------------------------------------------------------------------------
# Plumbing: idempotent retries and the webhook outbox
# ---------------------------------------------------------------------------

class IdempotencyRecord(models.Model):
    """The first answer to a write sent with an Idempotency-Key, replayed for retries."""

    platform = models.ForeignKey(Platform, on_delete=models.CASCADE, related_name='+')
    key = models.CharField(max_length=200)
    request_hash = models.CharField(max_length=64)
    status_code = models.PositiveSmallIntegerField(null=True)
    body = models.JSONField(null=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['platform', 'key'], name='idempotency_key_unique')]


class WebhookDelivery(models.Model):
    """One message to one platform, retried until it is accepted (an outbox)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    platform = models.ForeignKey(Platform, on_delete=models.CASCADE, related_name='deliveries')
    event_type = models.CharField(max_length=60)
    payload = models.JSONField()
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)  # None once delivered or given up
    delivered_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'webhook deliveries'

    def __str__(self):
        return f'{self.event_type} to {self.platform.slug}'
