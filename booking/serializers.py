"""
Request validation and response shapes.

Resource refs in a request are looked up ONLY among the calling platform's
resources. Another platform's ref is simply unknown here, so no request can
reach across platforms.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import serializers

from .holds import Requirement
from .models import Block, Booking, Closure, Resource, validate_timezone


class AwareDateTimeField(serializers.Field):
    """An ISO 8601 instant WITH its offset ('2026-12-01T10:00:00Z'). A bare local time is ambiguous: refused."""

    def to_internal_value(self, data):
        value = parse_datetime(str(data)) if data else None
        if value is None:
            raise serializers.ValidationError('Use ISO 8601, e.g. 2026-12-01T10:00:00Z.')
        if value.tzinfo is None:
            raise serializers.ValidationError('Include a UTC offset, e.g. 2026-12-01T10:00:00+00:00 or …Z.')
        return value.astimezone(UTC)

    def to_representation(self, value):
        return value.isoformat() if value else None


class DateOrDateTimeField(serializers.Field):
    """'2026-12-01' (a whole local day) or an ISO instant with offset."""

    def to_internal_value(self, data):
        text = str(data or '')
        if len(text) == 10 and (d := parse_date(text)):
            return d
        return AwareDateTimeField().to_internal_value(text)


class TimezoneField(serializers.CharField):
    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        try:
            validate_timezone(value)
        except Exception as exc:
            raise serializers.ValidationError(f'"{value}" is not an IANA time zone (e.g. "Africa/Accra").') from exc
        return value


def parse_hhmm(text: str, *, allow_24=False) -> int:
    try:
        h, m = (int(x) for x in str(text).split(':'))
    except ValueError as exc:
        raise serializers.ValidationError(f'"{text}" is not a time like "09:30".') from exc
    if (h, m) == (24, 0) and allow_24:
        return 1440
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise serializers.ValidationError(f'"{text}" is not a time like "09:30".')
    return h * 60 + m


def hhmm(minute: int) -> str:
    return f'{minute // 60:02d}:{minute % 60:02d}'


# ---------------------------------------------------------------------------
# Requirements: "room-a, and any one engineer"
# ---------------------------------------------------------------------------

class RequirementSerializer(serializers.Serializer):
    resources = serializers.ListField(child=serializers.CharField(max_length=120), required=False,
                                      max_length=200, help_text='Your refs. Omit to mean every resource of `kind`.')
    kind = serializers.SlugField(max_length=40, required=False)
    required = serializers.CharField(default='all', help_text='"all", or how many of them (e.g. 1).')

    def validate(self, attrs):
        if not attrs.get('resources') and not attrs.get('kind'):
            raise serializers.ValidationError('Give `resources`, `kind`, or both.')
        req = attrs['required']
        if req != 'all' and (not req.isdigit() or int(req) < 1):
            raise serializers.ValidationError({'required': '"all" or a whole number from 1.'})
        return attrs


def resolve_requirements(platform, groups: list[dict], *, active_only: bool) -> list[Requirement]:
    """Turn requirement groups into Resources of THIS platform."""
    out, seen = [], set()
    for i, g in enumerate(groups):
        qs = Resource.objects.filter(platform=platform)
        if g.get('kind'):
            qs = qs.filter(kind=g['kind'])
        if active_only and not g.get('resources'):
            qs = qs.filter(is_active=True)
        if g.get('resources'):
            refs = list(dict.fromkeys(g['resources']))
            found = {r.external_ref: r for r in qs.filter(external_ref__in=refs)}
            unknown = [ref for ref in refs if ref not in found]
            if unknown:
                raise serializers.ValidationError({'requirements': f'Group {i}: unknown resources {unknown}.'})
            resources = [found[ref] for ref in refs]
        else:
            resources = list(qs.order_by('name'))
        if not resources:
            raise serializers.ValidationError({'requirements': f'Group {i}: no resources match.'})
        if dupes := [r.external_ref for r in resources if r.id in seen]:
            raise serializers.ValidationError({'requirements': f'{dupes} appear in more than one group.'})
        seen.update(r.id for r in resources)
        required = len(resources) if g['required'] == 'all' else int(g['required'])
        if required > len(resources):
            raise serializers.ValidationError(
                {'requirements': f'Group {i} asks for {required} of {len(resources)} resources.'})
        out.append(Requirement(resources=resources, required=required))
    return out


# ---------------------------------------------------------------------------
# Resources and opening hours
# ---------------------------------------------------------------------------

class ResourceSerializer(serializers.ModelSerializer):
    ref = serializers.CharField(source='external_ref', read_only=True)
    timezone = TimezoneField(max_length=64, required=False)

    class Meta:
        model = Resource
        fields = ['ref', 'kind', 'name', 'timezone', 'is_active', 'buffer_before_minutes', 'buffer_after_minutes',
                  'min_notice_minutes', 'max_advance_days', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']


class SpanSerializer(serializers.Serializer):
    start = serializers.CharField(help_text='"09:00"')
    end = serializers.CharField(help_text='"18:00"; "24:00" for midnight at the end of the day')

    def validate(self, attrs):
        s, e = parse_hhmm(attrs['start']), parse_hhmm(attrs['end'], allow_24=True)
        if e <= s:
            raise serializers.ValidationError('end must be after start (use "24:00" for end of day).')
        return {'start': s, 'end': e}


class WeeklySerializer(SpanSerializer):
    weekday = serializers.IntegerField(min_value=0, max_value=6, help_text='0 = Monday … 6 = Sunday')

    def validate(self, attrs):
        return {'weekday': attrs['weekday'], **super().validate(attrs)}


class OverrideSerializer(serializers.Serializer):
    date = serializers.DateField()
    hours = SpanSerializer(many=True, help_text='Empty list: closed all day.')
    reason = serializers.CharField(max_length=200, required=False, default='', allow_blank=True)


class ScheduleSerializer(serializers.Serializer):
    weekly = WeeklySerializer(many=True)
    overrides = OverrideSerializer(many=True, required=False, default=list)


def schedule_out(r: Resource) -> dict:
    return {
        'weekly': [{'weekday': w.weekday, 'start': hhmm(w.start_minute), 'end': hhmm(w.end_minute)}
                   for w in r.weekly_hours.all()],
        'overrides': [{'date': o.date.isoformat(), 'reason': o.reason,
                       'hours': [{'start': hhmm(s), 'end': hhmm(e)} for s, e in o.hours]}
                      for o in r.overrides.all()],
    }


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

class AvailabilitySerializer(serializers.Serializer):
    requirements = RequirementSerializer(many=True, allow_empty=False, max_length=10)
    duration_minutes = serializers.IntegerField(min_value=5, max_value=7 * 24 * 60)
    start = DateOrDateTimeField(help_text='"2026-12-01" (from local midnight) or an instant with offset')
    end = DateOrDateTimeField(help_text='"2026-12-14" (through the end of that local day) or an instant')
    timezone = TimezoneField(max_length=64, required=False, help_text='For dates and the answer. Default: '
                             'the first resource\'s zone.')
    step_minutes = serializers.IntegerField(min_value=5, max_value=1440, default=30,
                                            help_text='Slots start every N minutes from local midnight.')
    include_unavailable = serializers.BooleanField(default=False)


def window(start, end, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Dates are whole local days (end date inclusive); instants are used as given."""
    if isinstance(start, date) and not isinstance(start, datetime):
        start = datetime.combine(start, time(), tz).astimezone(UTC)
    if isinstance(end, date) and not isinstance(end, datetime):
        end = datetime.combine(end + timedelta(days=1), time(), tz).astimezone(UTC)
    if end <= start:
        raise serializers.ValidationError({'end': 'end must be after start.'})
    if end - start > timedelta(days=settings.AVAILABILITY_MAX_DAYS):
        raise serializers.ValidationError({'end': f'Search at most {settings.AVAILABILITY_MAX_DAYS} days at once.'})
    return start, end


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------

class TimesMixin:
    def validate(self, attrs):
        if attrs['end'] <= attrs['start']:
            raise serializers.ValidationError({'end': 'end must be after start.'})
        if attrs['end'] - attrs['start'] > timedelta(days=31):
            raise serializers.ValidationError({'end': 'A booking can last at most 31 days.'})
        return attrs


class BookingCreateSerializer(TimesMixin, serializers.Serializer):
    requirements = RequirementSerializer(many=True, allow_empty=False, max_length=10)
    start = AwareDateTimeField()
    end = AwareDateTimeField()
    hold_minutes = serializers.IntegerField(min_value=1, required=False,
                                            help_text='How long the hold lasts. Default HOLD_DEFAULT_MINUTES.')
    confirm = serializers.BooleanField(default=False, help_text='Confirm at once (no payment step).')
    subject_ref = serializers.CharField(max_length=120, required=False, default='', allow_blank=True)
    external_ref = serializers.CharField(max_length=120, required=False, default='', allow_blank=True)
    metadata = serializers.JSONField(required=False, default=dict)
    override_rules = serializers.BooleanField(
        default=False, help_text='Staff bookings: ignore opening hours, notice, closures and inactive resources '
                                 '(never overlaps).')

    def validate_hold_minutes(self, value):
        if value > settings.HOLD_MAX_MINUTES:
            raise serializers.ValidationError(f'At most {settings.HOLD_MAX_MINUTES} minutes.')
        return value

    def validate_metadata(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('An object.')
        if len(str(value)) > 4000:
            raise serializers.ValidationError('Keep metadata under 4 KB.')
        return value


class ExtendSerializer(serializers.Serializer):
    minutes = serializers.IntegerField(min_value=1)

    def validate_minutes(self, value):
        if value > settings.HOLD_MAX_MINUTES:
            raise serializers.ValidationError(f'At most {settings.HOLD_MAX_MINUTES} minutes.')
        return value


class CancelSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=200, required=False, default='', allow_blank=True)


class RescheduleSerializer(TimesMixin, serializers.Serializer):
    start = AwareDateTimeField()
    end = AwareDateTimeField()
    requirements = RequirementSerializer(many=True, required=False, max_length=10,
                                         help_text='Omit to keep the same resources.')
    override_rules = serializers.BooleanField(default=False)


class BookingSerializer(serializers.ModelSerializer):
    start = AwareDateTimeField(source='start_at')
    end = AwareDateTimeField(source='end_at')
    resources = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = ['id', 'external_ref', 'subject_ref', 'status', 'start', 'end', 'hold_expires_at', 'resources',
                  'confirmed_at', 'cancelled_at', 'expired_at', 'cancel_reason', 'metadata', 'created_at',
                  'updated_at']

    def get_resources(self, b) -> list[dict]:
        return [{'ref': a.resource.external_ref, 'kind': a.resource.kind, 'name': a.resource.name,
                 'group': a.group}
                for a in sorted(b.allocations.all(), key=lambda a: (a.group, a.resource.external_ref))]


# ---------------------------------------------------------------------------
# Blocks and closures
# ---------------------------------------------------------------------------

class BlockCreateSerializer(TimesMixin, serializers.Serializer):
    resource = serializers.CharField(max_length=120)
    start = AwareDateTimeField()
    end = AwareDateTimeField()
    kind = serializers.SlugField(max_length=40, default='maintenance')
    reason = serializers.CharField(max_length=200, required=False, default='', allow_blank=True)
    external_ref = serializers.CharField(max_length=120, required=False, default='', allow_blank=True)


class BlockSerializer(serializers.ModelSerializer):
    resource = serializers.CharField(source='resource.external_ref')
    start = serializers.SerializerMethodField()
    end = serializers.SerializerMethodField()

    class Meta:
        model = Block
        fields = ['id', 'resource', 'start', 'end', 'kind', 'reason', 'external_ref', 'created_at']

    def get_start(self, b) -> str:
        return b.period.lower.isoformat()

    def get_end(self, b) -> str:
        return b.period.upper.isoformat()


class ClosureCreateSerializer(serializers.Serializer):
    start = AwareDateTimeField()
    end = AwareDateTimeField()
    kinds = serializers.ListField(child=serializers.SlugField(max_length=40), required=False, default=list,
                                 help_text='Only these kinds of resource. Empty: every kind.')
    resources = serializers.ListField(child=serializers.CharField(max_length=120), required=False, default=list,
                                      max_length=200, help_text='Only these resources (your refs). Empty: all.')
    kind = serializers.SlugField(max_length=40, default='closure', help_text='"holiday", "maintenance"…')
    reason = serializers.CharField(max_length=200, required=False, default='', allow_blank=True)
    external_ref = serializers.CharField(max_length=120, required=False, default='', allow_blank=True)

    def validate(self, attrs):
        if attrs['end'] <= attrs['start']:
            raise serializers.ValidationError({'end': 'end must be after start.'})
        return attrs


class ClosureSerializer(serializers.ModelSerializer):
    start = serializers.SerializerMethodField()
    end = serializers.SerializerMethodField()
    resources = serializers.SerializerMethodField()

    class Meta:
        model = Closure
        fields = ['id', 'external_ref', 'kind', 'start', 'end', 'kinds', 'resources', 'reason', 'created_at']

    def get_resources(self, c) -> list[str]:
        return sorted(r.external_ref for r in c.resources.all())

    def get_start(self, c) -> str:
        return c.period.lower.isoformat()

    def get_end(self, c) -> str:
        return c.period.upper.isoformat()
