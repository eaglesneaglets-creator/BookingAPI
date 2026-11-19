"""
Availability: "which of these times are free for these resources?"

Read-only. It answers from a snapshot, so two platforms' customers can both
see a slot as free; only one of them will be able to HOLD it (holds.py and
the database constraint decide that). Nothing here ever reserves anything.

Opening hours are wall-clock times in the resource's own zone, turned into
UTC per date, so a 09:00 opening stays 09:00 local across daylight saving.

When a slot is not available, `reason` is the first that applies, in order:

    too_soon       inside the resource's minimum notice
    too_far        beyond how far ahead the resource can be booked
    closed         a platform closure (holiday) covers it
    blocked        maintenance / time off on the resource
    booked         another booking (or an unexpired hold) has it
    outside_hours  the resource is not open then
"""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.backends.postgresql.psycopg_any import DateTimeTZRange
from django.db.models import Q

from .models import Allocation, BookingStatus, Closure, Resource

REASONS = ('too_soon', 'too_far', 'closed', 'blocked', 'booked', 'outside_hours')


# ---------------------------------------------------------------------------
# Opening hours
# ---------------------------------------------------------------------------

def local_to_utc(day: date, minute: int, tz: ZoneInfo) -> datetime:
    """`minute` minutes after local midnight on `day` (1440 = the next midnight), in UTC."""
    naive = datetime.combine(day, time()) + timedelta(minutes=minute)
    return naive.replace(tzinfo=tz).astimezone(UTC)


def merge(intervals):
    """Sort and join intervals that overlap or touch: Mon 00:00–24:00 + Tue 00:00–24:00 is one stretch."""
    out = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def open_intervals(resource: Resource, start: datetime, end: datetime, weekly=None, overrides=None):
    """The resource's opening hours between start and end, as merged UTC intervals."""
    tz = ZoneInfo(resource.timezone)
    weekly = resource.weekly_hours.all() if weekly is None else weekly
    overrides = resource.overrides.all() if overrides is None else overrides
    by_day = defaultdict(list)
    for w in weekly:
        by_day[w.weekday].append((w.start_minute, w.end_minute))
    by_date = {o.date: [tuple(h) for h in o.hours] for o in overrides}

    out = []
    day, last = start.astimezone(tz).date() - timedelta(days=1), end.astimezone(tz).date()
    while day <= last:
        for s, e in by_date.get(day, by_day[day.weekday()]):
            a, b = local_to_utc(day, s, tz), local_to_utc(day, e, tz)
            if b > start and a < end:
                out.append((max(a, start), min(b, end)))
        day += timedelta(days=1)
    return merge(out)


# ---------------------------------------------------------------------------
# One resource's timeline over a window
# ---------------------------------------------------------------------------

@dataclass
class Timeline:
    resource: Resource
    open: list = field(default_factory=list)       # [(a, b)] merged, disjoint
    closures: list = field(default_factory=list)   # [(a, b)]
    busy: list = field(default_factory=list)       # [(a, b, 'booked'|'blocked')] sorted, disjoint
    _open_starts: list = field(default_factory=list)
    _busy_starts: list = field(default_factory=list)

    def __post_init__(self):
        self._open_starts = [a for a, _ in self.open]
        self._busy_starts = [a for a, _, _ in self.busy]

    def rule_violation(self, start: datetime, end: datetime, now: datetime) -> str | None:
        """Why the resource cannot be booked for [start, end), ignoring other bookings."""
        r = self.resource
        if start < now + timedelta(minutes=r.min_notice_minutes):
            return 'too_soon'
        if start > now + timedelta(days=r.max_advance_days):
            return 'too_far'
        if any(a < end and b > start for a, b in self.closures):
            return 'closed'
        i = bisect_right(self._open_starts, start) - 1
        if i < 0 or self.open[i][1] < end:
            return 'outside_hours'
        return None

    def busy_reason(self, start: datetime, end: datetime) -> str | None:
        """'blocked' / 'booked' if the period (with this resource's buffers) meets another allocation."""
        r = self.resource
        bs = start - timedelta(minutes=r.buffer_before_minutes)
        be = end + timedelta(minutes=r.buffer_after_minutes)
        # Disjoint and sorted by start, so ends are sorted too: the overlaps
        # are the run just before the first interval starting at or after be.
        kinds = set()
        i = bisect_left(self._busy_starts, be) - 1
        while i >= 0 and self.busy[i][1] > bs:
            kinds.add(self.busy[i][2])
            i -= 1
        if 'blocked' in kinds:
            return 'blocked'
        return 'booked' if kinds else None

    def state(self, start: datetime, end: datetime, now: datetime) -> str | None:
        v = self.rule_violation(start, end, now)
        if v in ('too_soon', 'too_far', 'closed'):
            return v
        return self.busy_reason(start, end) or v


def build_timelines(platform, resources, start: datetime, end: datetime, now: datetime, *, include_busy=True):
    """Timelines for `resources` (all of `platform`) covering [start, end)."""
    resources = list(resources)
    if not resources:
        return {}
    pad = timedelta(minutes=max(max(r.buffer_before_minutes, r.buffer_after_minutes) for r in resources))
    window = DateTimeTZRange(start - pad, end + pad, '[)')
    ids = [r.id for r in resources]
    full = {r.id: r for r in Resource.objects.filter(platform=platform, id__in=ids)
            .prefetch_related('weekly_hours', 'overrides')}

    closures = list(Closure.objects.filter(platform=platform, period__overlap=window))
    busy = defaultdict(list)
    if include_busy:
        # An overdue hold no longer counts: whoever holds next expires it.
        overdue = Q(booking__status=BookingStatus.HELD, booking__hold_expires_at__lte=now)
        allocations = (Allocation.objects.filter(platform=platform, resource_id__in=ids, active=True,
                                                 period__overlap=window)
                       .filter(Q(block__isnull=False) | ~overdue)
                       .values_list('resource_id', 'period', 'block_id'))
        for rid, period, block_id in allocations:
            busy[rid].append((period.lower, period.upper, 'blocked' if block_id else 'booked'))

    out = {}
    for rid in ids:
        r = full[rid]
        out[rid] = Timeline(
            resource=r,
            open=open_intervals(r, start, end, r.weekly_hours.all(), r.overrides.all()),
            closures=[(c.period.lower, c.period.upper) for c in closures if not c.kinds or r.kind in c.kinds],
            busy=sorted(busy[rid]),
        )
    return out


# ---------------------------------------------------------------------------
# The search
# ---------------------------------------------------------------------------

def slot_starts(start: datetime, end: datetime, duration: timedelta, step_minutes: int, tz: ZoneInfo):
    """Slot starts on a wall-clock grid (every `step` minutes from local midnight) inside [start, end - duration]."""
    out = []
    day, last = start.astimezone(tz).date(), end.astimezone(tz).date()
    while day <= last:
        for minute in range(0, 1440, step_minutes):
            t = local_to_utc(day, minute, tz)
            if t >= start and t + duration <= end:
                out.append(t)
        day += timedelta(days=1)
    return sorted(set(out))


def search(platform, requirements, start: datetime, end: datetime, *, duration_minutes: int, step_minutes: int,
           tz: ZoneInfo, now: datetime, include_unavailable: bool = False) -> list[dict]:
    duration = timedelta(minutes=duration_minutes)
    resources = [r for req in requirements for r in req.resources]
    timelines = build_timelines(platform, resources, start, end, now)
    slots = []
    for t in slot_starts(start, end, duration, step_minutes, tz):
        t_end = t + duration
        free, reason = [], None
        for req in requirements:
            states = {r.external_ref: timelines[r.id].state(t, t_end, now) for r in req.resources}
            ok = [ref for ref, s in states.items() if s is None]
            free.append(ok)
            if len(ok) < req.required and reason is None:
                found = [s for s in states.values() if s is not None]
                reason = min(found, key=REASONS.index) if found else 'booked'
        if reason is None or include_unavailable:
            slot = {'start': t.astimezone(tz).isoformat(), 'end': t_end.astimezone(tz).isoformat(),
                    'available': reason is None, 'free': free}
            if reason:
                slot['reason'] = reason
            slots.append(slot)
    return slots
