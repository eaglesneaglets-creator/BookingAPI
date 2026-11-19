"""
Holding, confirming, rescheduling and cancelling time — safely under concurrency.

HOW TWO CUSTOMERS CAN'T GET THE SAME HOUR

1. Postgres decides. Taking time means inserting an active Allocation, and the
   `no_double_booking` exclusion constraint refuses an overlapping one for the
   same resource. When two transactions race for the same slot, the second
   insert waits for the first to commit, then fails (SQLSTATE 23P01). There is
   no "check, then write" gap for a race to slip through.

2. All or nothing. A booking's allocations (room + engineer) are inserted in
   one transaction; if any is refused, all of them roll back.

3. "Any one of these" tries candidates one by one, each in a savepoint, so a
   refused candidate is undone on its own and the next one is tried.

4. No deadlocks. Two bookings wanting the same room and engineer in opposite
   orders would deadlock on the constraint check. So before writing, every
   transaction takes a lock per resource it may touch (a Postgres advisory
   lock, released at commit), always in one global order: a cycle of waits
   cannot form. Writes on the same resource queue for a few milliseconds;
   writes on different resources never wait for each other (or across
   platforms). If Postgres still aborts one for a deadlock or serialization
   failure, it is run again.

5. Every change to an existing booking locks its row first (SELECT … FOR
   UPDATE), so a confirm, a cancel and the expiry sweeper can never interleave
   on one booking.

6. Expiry is a status change, not a clock inside the constraint (Postgres
   can't use now() there). The sweeper expires overdue holds every minute,
   and a hold that is refused first expires any overdue hold in its way, so a
   late sweeper never costs anyone a slot.

7. A late confirmation is honoured if it can be. Confirming an expired hold
   takes its time back if nobody else has it; if someone does, the answer is
   409 `slot_taken`, so the platform refunds instead of charging for nothing.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.backends.postgresql.psycopg_any import DateTimeTZRange
from django.utils import timezone
from rest_framework.exceptions import NotFound

from . import availability, webhooks
from .errors import Conflict, SlotTaken, SlotUnavailable, WrongStatus
from .models import Allocation, Block, Booking, BookingStatus, Platform, Resource

EXCLUSION_VIOLATION = '23P01'
RETRYABLE = {'40P01', '40001'}  # deadlock, serialization failure
ATTEMPTS = 3


@dataclass
class Requirement:
    """One group of a booking: `required` of these resources (all of them, or any N)."""

    resources: list[Resource]
    required: int


def period(start: datetime, end: datetime) -> DateTimeTZRange:
    return DateTimeTZRange(start, end, '[)')


def allocation_period(r: Resource, start: datetime, end: datetime) -> DateTimeTZRange:
    return period(start - timedelta(minutes=r.buffer_before_minutes),
                  end + timedelta(minutes=r.buffer_after_minutes))


def _sqlstate(exc: Exception) -> str | None:
    return getattr(exc.__cause__, 'sqlstate', None)


def lock_resources(resources) -> None:
    """Advisory-lock these resources in one global order (see 4. above). Inside a transaction only."""
    keys = sorted({int.from_bytes(r.id.bytes[:8], 'big', signed=True) for r in resources})
    if keys:
        with connection.cursor() as cur:
            # The inner ORDER BY fixes the order the locks are taken in.
            cur.execute('SELECT count(pg_advisory_xact_lock(k)) FROM '
                        '(SELECT k FROM unnest(%s::bigint[]) AS k ORDER BY k) AS ordered', [keys])


def with_retries(fn):
    """Run fn (one transaction) again if Postgres aborted it for a deadlock."""
    for attempt in range(ATTEMPTS):
        try:
            return fn()
        except DatabaseError as exc:
            if _sqlstate(exc) not in RETRYABLE or attempt == ATTEMPTS - 1:
                raise
    return None  # unreachable


def candidate_order(resources: list[Resource], start: datetime, end: datetime) -> list[Resource]:
    """
    The order in which "any N of these" candidates are tried, so it decides
    WHO gets the booking when several are free (which engineer, which mentor).

    Rule: LEAST BOOKED THAT DAY first, so work is spread evenly. "That day"
    is the local calendar day (in each resource's own zone) on which the
    booking starts; "booked" is the minutes of held or confirmed bookings
    (not blocks: time off is not work). Ties keep the order the platform sent,
    so a platform can still express a preference among equally busy people.

    It runs inside the hold's transaction, after the resources are locked
    (lock_resources), so the counts cannot change underneath it. Read-only.
    """
    if len(resources) < 2:
        return list(resources)
    days = {}
    for r in resources:
        tz = ZoneInfo(r.timezone)
        local_day = start.astimezone(tz).date()
        days[r.id] = (datetime.combine(local_day, time(), tz), datetime.combine(local_day + timedelta(days=1), time(), tz))
    lo, hi = min(d[0] for d in days.values()), max(d[1] for d in days.values())
    booked = dict.fromkeys(days, 0.0)
    for rid, p in (Allocation.objects
                   .filter(resource__in=resources, active=True, booking__isnull=False, period__overlap=period(lo, hi))
                   .values_list('resource_id', 'period')):
        day_start, day_end = days[rid]
        overlap = min(p.upper, day_end) - max(p.lower, day_start)
        booked[rid] += max(overlap.total_seconds(), 0) / 60
    position = {r.id: i for i, r in enumerate(resources)}
    return sorted(resources, key=lambda r: (booked[r.id], position[r.id]))


def lock(platform: Platform, booking_id) -> Booking:
    b = Booking.objects.select_for_update().filter(platform=platform, pk=booking_id).first()
    if b is None:
        raise NotFound('No such booking.')
    return b


def _expire(b: Booking, now: datetime) -> None:
    b.status, b.expired_at = BookingStatus.EXPIRED, now
    b.save(update_fields=['status', 'expired_at', 'updated_at'])
    b.allocations.update(active=False)
    webhooks.enqueue(b, 'booking.expired')


def _clear_overdue(platform: Platform, resources, start: datetime, end: datetime, now: datetime) -> None:
    """Expire overdue holds that would stand in the way of [start, end) on these resources."""
    if not resources:
        return
    pad = timedelta(minutes=max(max(r.buffer_before_minutes, r.buffer_after_minutes) for r in resources))
    ids = (Allocation.objects
           .filter(platform=platform, active=True, resource__in=resources,
                   period__overlap=period(start - pad, end + pad),
                   booking__status=BookingStatus.HELD, booking__hold_expires_at__lte=now)
           .values_list('booking_id', flat=True).distinct())
    # skip_locked: a hold someone is confirming right now is theirs to settle.
    for b in Booking.objects.select_for_update(skip_locked=True).filter(
            pk__in=list(ids), status=BookingStatus.HELD, hold_expires_at__lte=now):
        _expire(b, now)


def _allocate(platform, booking, requirements, start, end, now, override_rules) -> None:
    """Take time for every requirement group, or raise SlotUnavailable (caller's transaction rolls back)."""
    timelines = availability.build_timelines(
        platform, [r for req in requirements for r in req.resources], start, end, now, include_busy=False)
    for gi, req in enumerate(requirements):
        everyone = req.required == len(req.resources)
        order = sorted(req.resources, key=lambda r: r.id) if everyone else candidate_order(req.resources, start, end)
        chosen, reasons = 0, {}
        for r in order:
            if chosen == req.required:
                break
            # Staff (override_rules) may still book a resource taken off sale:
            # bookings made before it was switched off stay valid on the platform.
            if not r.is_active and not override_rules:
                reasons[r.external_ref] = 'inactive'
                continue
            if not override_rules:
                why = timelines[r.id].rule_violation(start, end, now)
                if why:
                    reasons[r.external_ref] = why
                    continue
            try:
                with transaction.atomic():  # savepoint: a refusal undoes only this one
                    Allocation.objects.create(platform=platform, resource=r, booking=booking, group=gi,
                                              period=allocation_period(r, start, end))
            except IntegrityError as exc:
                if _sqlstate(exc) != EXCLUSION_VIOLATION:
                    raise
                reasons[r.external_ref] = 'booked'
                continue
            chosen += 1
        if chosen < req.required:
            raise SlotUnavailable(details={'group': gi, 'required': req.required, 'reasons': reasons})


# ---------------------------------------------------------------------------
# Booking lifecycle
# ---------------------------------------------------------------------------

def create(platform: Platform, *, requirements: list[Requirement], start: datetime, end: datetime,
           hold_minutes: int, confirm: bool = False, subject_ref: str = '', external_ref: str = '',
           metadata: dict | None = None, override_rules: bool = False) -> Booking:
    def attempt():
        with transaction.atomic():
            everything = [r for req in requirements for r in req.resources]
            lock_resources(everything)
            now = timezone.now()
            _clear_overdue(platform, everything, start, end, now)
            b = Booking(platform=platform, subject_ref=subject_ref, external_ref=external_ref,
                        metadata=metadata or {}, start_at=start, end_at=end)
            if confirm:
                b.status, b.confirmed_at = BookingStatus.CONFIRMED, now
            else:
                b.status, b.hold_expires_at = BookingStatus.HELD, now + timedelta(minutes=hold_minutes)
            try:
                with transaction.atomic():
                    b.save()
            except IntegrityError as exc:
                raise Conflict('A booking with this external_ref already exists.', 'duplicate_external_ref') from exc
            _allocate(platform, b, requirements, start, end, now, override_rules)
            webhooks.enqueue(b, 'booking.confirmed' if confirm else 'booking.held')
            return b
    return with_retries(attempt)


def _take_back(b: Booking, now: datetime) -> None:
    """Re-activate an expired booking's allocations, or raise SlotTaken."""
    resources = [a.resource for a in b.allocations.select_related('resource')]
    lock_resources(resources)
    _clear_overdue(b.platform, resources, b.start_at, b.end_at, now)
    try:
        with transaction.atomic():
            b.allocations.update(active=True)
    except IntegrityError as exc:
        if _sqlstate(exc) != EXCLUSION_VIOLATION:
            raise
        raise SlotTaken() from exc


def confirm(platform: Platform, booking_id) -> Booking:
    def attempt():
        with transaction.atomic():
            now = timezone.now()
            b = lock(platform, booking_id)
            if b.status == BookingStatus.CONFIRMED:
                return b
            if b.status == BookingStatus.CANCELLED:
                raise WrongStatus('A cancelled booking cannot be confirmed.')
            if b.status == BookingStatus.EXPIRED:
                _take_back(b, now)
            # An overdue hold that nobody has expired yet still owns its time: confirm it.
            b.status, b.confirmed_at, b.hold_expires_at = BookingStatus.CONFIRMED, now, None
            b.save(update_fields=['status', 'confirmed_at', 'hold_expires_at', 'updated_at'])
            webhooks.enqueue(b, 'booking.confirmed')
            return b
    return with_retries(attempt)


def extend(platform: Platform, booking_id, minutes: int) -> Booking:
    """Keep a hold longer (payment still in progress). An expired hold is taken back if still free."""
    def attempt():
        with transaction.atomic():
            now = timezone.now()
            b = lock(platform, booking_id)
            if b.status not in (BookingStatus.HELD, BookingStatus.EXPIRED):
                raise WrongStatus(f'Only a held booking can be extended; this one is {b.status}.')
            if b.status == BookingStatus.EXPIRED:
                _take_back(b, now)
                b.status, b.expired_at = BookingStatus.HELD, None
            b.hold_expires_at = now + timedelta(minutes=minutes)
            b.save(update_fields=['status', 'expired_at', 'hold_expires_at', 'updated_at'])
            return b
    return with_retries(attempt)


def cancel(platform: Platform, booking_id, reason: str = '') -> Booking:
    with transaction.atomic():
        now = timezone.now()
        b = lock(platform, booking_id)
        if b.status in (BookingStatus.CANCELLED, BookingStatus.EXPIRED):
            return b  # already released: cancelling again changes nothing
        b.status, b.cancelled_at, b.cancel_reason = BookingStatus.CANCELLED, now, reason[:200]
        b.save(update_fields=['status', 'cancelled_at', 'cancel_reason', 'updated_at'])
        b.allocations.update(active=False)
        webhooks.enqueue(b, 'booking.cancelled')
        return b


def current_requirements(b: Booking) -> list[Requirement]:
    groups = {}
    for a in b.allocations.select_related('resource').order_by('group'):
        groups.setdefault(a.group, []).append(a.resource)
    return [Requirement(resources=rs, required=len(rs)) for _, rs in sorted(groups.items())]


def reschedule(platform: Platform, booking_id, *, start: datetime, end: datetime,
               requirements: list[Requirement] | None = None, override_rules: bool = False) -> Booking:
    """Move a booking (and optionally change its resources). If the new time is taken, nothing changes."""
    def attempt():
        with transaction.atomic():
            now = timezone.now()
            b = lock(platform, booking_id)
            if b.status == BookingStatus.HELD and b.hold_expires_at <= now:
                raise WrongStatus('This hold has run out; hold the new time instead.', 'hold_expired')
            if b.status not in (BookingStatus.HELD, BookingStatus.CONFIRMED):
                raise WrongStatus(f'A {b.status} booking cannot be moved.')
            old = current_requirements(b)
            reqs = requirements or old
            wanted = [r for req in reqs for r in req.resources]
            lock_resources(wanted + [r for req in old for r in req.resources])
            b.allocations.all().delete()
            _clear_overdue(platform, wanted, start, end, now)
            _allocate(platform, b, reqs, start, end, now, override_rules)
            b.start_at, b.end_at = start, end
            b.save(update_fields=['start_at', 'end_at', 'updated_at'])
            webhooks.enqueue(b, 'booking.rescheduled')
            return b
    return with_retries(attempt)


def expire_overdue(limit: int = 500) -> int:
    """The sweeper: expire holds whose time ran out. Safe to run on many workers at once."""
    with transaction.atomic():
        now = timezone.now()
        due = list(Booking.objects.select_for_update(skip_locked=True)
                   .filter(status=BookingStatus.HELD, hold_expires_at__lte=now).order_by('hold_expires_at')[:limit])
        for b in due:
            _expire(b, now)
        return len(due)


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------

def create_block(platform: Platform, resource: Resource, *, start: datetime, end: datetime, kind: str,
                 reason: str = '', external_ref: str = '') -> Block:
    """Take a resource out of use. Refused (409) if bookings already have that time; they are listed."""
    def attempt():
        with transaction.atomic():
            lock_resources([resource])
            now = timezone.now()
            _clear_overdue(platform, [resource], start, end, now)
            blk = Block.objects.create(platform=platform, resource=resource, period=period(start, end),
                                       kind=kind, reason=reason, external_ref=external_ref)
            try:
                with transaction.atomic():
                    Allocation.objects.create(platform=platform, resource=resource, block=blk,
                                              period=period(start, end))
            except IntegrityError as exc:
                if _sqlstate(exc) != EXCLUSION_VIOLATION:
                    raise
                clashes = (Allocation.objects.filter(resource=resource, active=True,
                                                     period__overlap=period(start, end))
                           .select_related('booking', 'block'))
                raise SlotUnavailable(
                    'Bookings or blocks already have part of this time.',
                    details={'conflicts': [
                        {'booking_id': str(a.booking_id), 'external_ref': a.booking.external_ref}
                        if a.booking_id else {'block_id': str(a.block_id)} for a in clashes]},
                ) from exc
            return blk
    return with_retries(attempt)
