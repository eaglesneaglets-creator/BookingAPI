"""
Real races: many threads, each with its own database connection and its own
transactions, released at the same instant by a barrier. These tests use
committed data (transactional_db), so Postgres locking is exercised for real.
"""

import threading
from collections import Counter
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from booking import holds
from booking.models import Allocation, Booking, Platform
from conftest import add_resource, client_for, day, iso

pytestmark = pytest.mark.django_db(transaction=True)


def race(n, fn):
    """Run fn(i) in n threads started together; return their results (exceptions included)."""
    barrier, results = threading.Barrier(n), [None] * n

    def run(i):
        try:
            barrier.wait()
            results[i] = fn(i)
        except Exception as exc:  # noqa: BLE001 — reported by the test
            results[i] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results


@pytest.fixture
def platform():
    return Platform.objects.create(slug='race', name='Race')


def body(groups, start, end, **extra):
    reqs = [g if isinstance(g, dict) else {'resources': g} for g in groups]
    return {'requirements': reqs, 'start': iso(start), 'end': iso(end), **extra}


def test_thirty_customers_one_slot_exactly_one_wins(platform):
    api = client_for(platform)
    add_resource(api, 'room-a')
    clients = [client_for(platform) for _ in range(30)]
    codes = race(30, lambda i: clients[i].post('/v1/bookings/', body([['room-a']], day(3, 10), day(3, 12)))
                 .status_code)
    assert Counter(codes) == {201: 1, 409: 29}
    assert Allocation.objects.filter(active=True).count() == 1


def test_overlapping_but_different_times_still_only_one(platform):
    api = client_for(platform)
    add_resource(api, 'room-a')
    clients = [client_for(platform) for _ in range(12)]
    # 10:00–12:00, 10:10–12:10, … all overlap each other.
    codes = race(12, lambda i: clients[i].post(
        '/v1/bookings/', body([['room-a']], day(3, 10, 10 * i), day(3, 12, 10 * i))).status_code)
    assert Counter(codes) == {201: 1, 409: 11}


def test_room_and_engineer_in_opposite_orders_no_deadlock_no_double(platform):
    api = client_for(platform)
    add_resource(api, 'room-a')
    add_resource(api, 'eng-1', kind='engineer')
    clients = [client_for(platform) for _ in range(16)]

    def go(i):
        groups = [['room-a'], ['eng-1']] if i % 2 else [['eng-1'], ['room-a']]
        return clients[i].post('/v1/bookings/', body(groups, day(3, 10), day(3, 12))).status_code

    codes = race(16, go)
    assert Counter(codes) == {201: 1, 409: 15}
    assert Allocation.objects.filter(active=True).count() == 2  # one room + one engineer, same booking


def test_any_engineer_three_engineers_ten_customers(platform):
    api = client_for(platform)
    for e in ('eng-1', 'eng-2', 'eng-3'):
        add_resource(api, e, kind='engineer')
    clients = [client_for(platform) for _ in range(10)]
    responses = race(10, lambda i: clients[i].post(
        '/v1/bookings/', body([{'kind': 'engineer', 'required': 1}], day(3, 10), day(3, 11))))
    won = [r for r in responses if r.status_code == 201]
    assert len(won) == 3
    assert sorted(r.data['resources'][0]['ref'] for r in won) == ['eng-1', 'eng-2', 'eng-3']
    assert all(r.status_code in (201, 409) for r in responses)


def test_same_idempotency_key_at_the_same_instant_books_once(platform):
    api = client_for(platform)
    add_resource(api, 'room-a')
    clients = [client_for(platform) for _ in range(10)]
    payload = body([['room-a']], day(3, 10), day(3, 12))
    responses = race(10, lambda i: clients[i].post('/v1/bookings/', payload, HTTP_IDEMPOTENCY_KEY='pay-123'))
    assert {r.status_code for r in responses} == {201}
    assert len({r.data['id'] for r in responses}) == 1
    assert Booking.objects.count() == 1


def test_confirm_racing_the_sweeper_never_leaves_a_half_state(platform):
    api = client_for(platform)
    add_resource(api, 'room-a')
    for round_ in range(8):
        start = day(3 + round_, 10)
        b = api.post('/v1/bookings/', body([['room-a']], start, start + timedelta(hours=1))).data
        Booking.objects.filter(pk=b['id']).update(hold_expires_at=timezone.now() - timedelta(seconds=1))
        race(2, lambda i, b=b: holds.confirm(platform, b['id']) if i == 0 else holds.expire_overdue())
        final = Booking.objects.get(pk=b['id'])
        active = Allocation.objects.filter(booking_id=b['id'], active=True).exists()
        # Whoever won, the booking and its time agree.
        assert (final.status, active) in {('confirmed', True), ('expired', False)}, round_


def test_late_confirm_racing_a_new_customer_one_gets_the_slot(platform):
    api = client_for(platform)
    add_resource(api, 'room-a')
    start, end = day(3, 10), day(3, 12)
    b = api.post('/v1/bookings/', body([['room-a']], start, end)).data
    Booking.objects.filter(pk=b['id']).update(status='expired', hold_expires_at=timezone.now())
    Allocation.objects.filter(booking_id=b['id']).update(active=False)
    other = client_for(platform)

    def go(i):
        if i == 0:
            try:
                holds.confirm(platform, b['id'])
                return 'confirmed'
            except holds.SlotTaken:
                return 'slot_taken'
        return other.post('/v1/bookings/', body([['room-a']], start, end)).status_code

    results = race(2, go)
    assert results in (['confirmed', 409], ['slot_taken', 201])
    assert Allocation.objects.filter(active=True).count() == 1
