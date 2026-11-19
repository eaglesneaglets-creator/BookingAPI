import json
from datetime import timedelta

from django.utils import timezone

from booking import holds, webhooks
from booking.models import Allocation, Booking
from conftest import add_resource, day, hold, iso


def studio(api):
    add_resource(api, 'room-a')
    for e in ('eng-1', 'eng-2'):
        add_resource(api, e, kind='engineer')


def overdue(booking_id):
    Booking.objects.filter(pk=booking_id).update(hold_expires_at=timezone.now() - timedelta(minutes=1))


# --- holding -----------------------------------------------------------------

def test_hold_room_and_any_engineer(api):
    studio(api)
    r = hold(api, [['room-a'], {'kind': 'engineer', 'required': 1}], day(3, 10), day(3, 13),
             subject_ref='user-7', external_ref='ONA-7F3A2C', hold_minutes=120)
    assert r.status_code == 201, r.data
    assert r.data['status'] == 'held'
    assert [(x['ref'], x['group']) for x in r.data['resources']] == [('room-a', 0), ('eng-1', 1)]
    assert r.data['external_ref'] == 'ONA-7F3A2C'


def test_the_same_room_cannot_be_held_twice(api):
    studio(api)
    assert hold(api, [['room-a']], day(3, 10), day(3, 12)).status_code == 201
    r = hold(api, [['room-a']], day(3, 11), day(3, 13))
    assert r.status_code == 409
    assert r.data['code'] == 'slot_unavailable'
    assert r.data['details'] == {'group': 0, 'required': 1, 'reasons': {'room-a': 'booked'}}


def test_back_to_back_is_fine(api):
    studio(api)
    assert hold(api, [['room-a']], day(3, 10), day(3, 12)).status_code == 201
    assert hold(api, [['room-a']], day(3, 12), day(3, 14)).status_code == 201


def test_any_engineer_skips_the_busy_one(api):
    studio(api)
    assert hold(api, [['eng-1']], day(3, 10), day(3, 12)).status_code == 201
    r = hold(api, [{'kind': 'engineer', 'required': 1}], day(3, 10), day(3, 12))
    assert [x['ref'] for x in r.data['resources']] == ['eng-2']
    r = hold(api, [{'kind': 'engineer', 'required': 1}], day(3, 10), day(3, 12))
    assert r.status_code == 409
    assert r.data['details']['reasons'] == {'eng-1': 'booked', 'eng-2': 'booked'}


def test_any_engineer_goes_to_the_least_booked_that_day(api):
    for e in ('eng-1', 'eng-2', 'eng-3'):
        add_resource(api, e, kind='engineer')
    assert hold(api, [['eng-1']], day(3, 8), day(3, 11)).status_code == 201   # 3 h
    assert hold(api, [['eng-2']], day(3, 8), day(3, 9)).status_code == 201    # 1 h
    assert hold(api, [['eng-3']], day(3, 9), day(3, 11)).status_code == 201   # 2 h
    assert hold(api, [['eng-2']], day(4, 8), day(4, 18)).status_code == 201   # another day: does not count
    r = hold(api, [{'kind': 'engineer', 'required': 1}], day(3, 14), day(3, 15))
    assert [x['ref'] for x in r.data['resources']] == ['eng-2']
    # Now eng-2 has 2 h, tied with eng-3: the order sent breaks the tie.
    r = hold(api, [{'resources': ['eng-3', 'eng-2', 'eng-1'], 'required': 1}], day(3, 16), day(3, 17))
    assert [x['ref'] for x in r.data['resources']] == ['eng-3']


def test_all_or_nothing(api):
    studio(api)
    assert hold(api, [['eng-1']], day(3, 10), day(3, 12)).status_code == 201
    # The room is free but the engineer is not: nothing is taken.
    assert hold(api, [['room-a'], ['eng-1']], day(3, 10), day(3, 12)).status_code == 409
    assert hold(api, [['room-a']], day(3, 10), day(3, 12)).status_code == 201
    assert Booking.objects.count() == 2


def test_buffers_really_block(api):
    add_resource(api, 'room-c', buffer_after_minutes=30)
    assert hold(api, [['room-c']], day(3, 10), day(3, 12)).status_code == 201
    assert hold(api, [['room-c']], day(3, 12), day(3, 13)).status_code == 409  # cleaning
    assert hold(api, [['room-c']], day(3, 12, 30), day(3, 13)).status_code == 201


def test_rules_opening_hours_notice_and_closures(api):
    add_resource(api, 'mentor', kind='mentor', min_notice_minutes=24 * 60,
                 hours={'weekly': [{'weekday': d, 'start': '09:00', 'end': '17:00'} for d in range(7)]})
    r = hold(api, [['mentor']], day(3, 18), day(3, 19))
    assert r.data['details']['reasons'] == {'mentor': 'outside_hours'}
    r = hold(api, [['mentor']], timezone.now() + timedelta(hours=1), timezone.now() + timedelta(hours=2))
    assert r.data['details']['reasons'] == {'mentor': 'too_soon'}
    api.post('/v1/closures/', {'start': iso(day(4)), 'end': iso(day(5)), 'reason': 'Holiday'})
    r = hold(api, [['mentor']], day(4, 10), day(4, 11))
    assert r.data['details']['reasons'] == {'mentor': 'closed'}
    # Staff can book outside the rules, but never on top of another booking.
    assert hold(api, [['mentor']], day(3, 18), day(3, 19), override_rules=True).status_code == 201
    assert hold(api, [['mentor']], day(3, 18), day(3, 19), override_rules=True).status_code == 409


def test_inactive_resources_only_for_staff(api):
    add_resource(api, 'old-room', is_active=False)
    r = hold(api, [['old-room']], day(3, 10), day(3, 11))
    assert r.data['details']['reasons'] == {'old-room': 'inactive'}
    # A platform mirroring bookings made before the room was switched off.
    assert hold(api, [['old-room']], day(3, 10), day(3, 11), override_rules=True).status_code == 201
    assert hold(api, [['old-room']], day(3, 10), day(3, 11), override_rules=True).status_code == 409


def test_overnight_session_across_midnight(api):
    add_resource(api, 'room-a')
    assert hold(api, [['room-a']], day(3, 22), day(4, 2)).status_code == 201


def test_duplicate_external_ref(api):
    studio(api)
    assert hold(api, [['room-a']], day(3, 10), day(3, 11), external_ref='X1').status_code == 201
    r = hold(api, [['eng-1']], day(3, 10), day(3, 11), external_ref='X1')
    assert (r.status_code, r.data['code']) == (409, 'duplicate_external_ref')


def test_naive_times_are_refused(api):
    studio(api)
    r = api.post('/v1/bookings/', {'requirements': [{'resources': ['room-a']}],
                                   'start': '2030-01-01T10:00:00', 'end': '2030-01-01T11:00:00'})
    assert r.status_code == 400


# --- lifecycle ---------------------------------------------------------------

def test_confirm_and_cancel(api, webhook_calls):
    studio(api)
    b = hold(api, [['room-a']], day(3, 10), day(3, 12)).data
    r = api.post(f'/v1/bookings/{b["id"]}/confirm/')
    assert (r.data['status'], r.data['hold_expires_at']) == ('confirmed', None)
    assert api.post(f'/v1/bookings/{b["id"]}/confirm/').data['status'] == 'confirmed'  # again: no-op
    r = api.post(f'/v1/bookings/{b["id"]}/cancel/', {'reason': 'Customer asked'})
    assert r.data['status'] == 'cancelled'
    assert api.post(f'/v1/bookings/{b["id"]}/cancel/', {}).data['status'] == 'cancelled'
    r = api.post(f'/v1/bookings/{b["id"]}/confirm/')
    assert (r.status_code, r.data['code']) == (409, 'wrong_status')
    # The time is free again.
    assert hold(api, [['room-a']], day(3, 10), day(3, 12)).status_code == 201
    assert [json.loads(c['body'])['type'] for c in webhook_calls] == [
        'booking.held', 'booking.confirmed', 'booking.cancelled', 'booking.held']


def test_an_overdue_hold_gives_way_to_the_next_customer(api, webhook_calls):
    studio(api)
    first = hold(api, [['room-a']], day(3, 10), day(3, 12)).data
    overdue(first['id'])
    assert hold(api, [['room-a']], day(3, 10), day(3, 12)).status_code == 201
    assert Booking.objects.get(pk=first['id']).status == 'expired'
    assert 'booking.expired' in [json.loads(c['body'])['type'] for c in webhook_calls]


def test_the_sweeper_expires_overdue_holds(api):
    studio(api)
    b = hold(api, [['room-a']], day(3, 10), day(3, 12)).data
    keep = hold(api, [['eng-1']], day(3, 10), day(3, 12)).data
    overdue(b['id'])
    assert holds.expire_overdue() == 1
    assert Booking.objects.get(pk=b['id']).status == 'expired'
    assert not Allocation.objects.filter(booking_id=b['id'], active=True).exists()
    assert Booking.objects.get(pk=keep['id']).status == 'held'


def test_late_payment_takes_the_time_back_if_still_free(api):
    studio(api)
    b = hold(api, [['room-a']], day(3, 10), day(3, 12)).data
    overdue(b['id'])
    holds.expire_overdue()
    r = api.post(f'/v1/bookings/{b["id"]}/confirm/')
    assert r.status_code == 200 and r.data['status'] == 'confirmed'


def test_late_payment_when_the_time_is_gone_says_slot_taken(api):
    studio(api)
    b = hold(api, [['room-a']], day(3, 10), day(3, 12)).data
    overdue(b['id'])
    assert hold(api, [['room-a']], day(3, 11), day(3, 13)).status_code == 201
    r = api.post(f'/v1/bookings/{b["id"]}/confirm/')
    assert (r.status_code, r.data['code']) == (409, 'slot_taken')
    assert Booking.objects.get(pk=b['id']).status == 'expired'


def test_extend(api):
    studio(api)
    b = hold(api, [['room-a']], day(3, 10), day(3, 12), hold_minutes=5).data
    r = api.post(f'/v1/bookings/{b["id"]}/extend/', {'minutes': 60})
    assert r.data['hold_expires_at'] > b['hold_expires_at']


def test_reschedule(api):
    studio(api)
    b = hold(api, [['room-a'], {'kind': 'engineer', 'required': 1}], day(3, 10), day(3, 12)).data
    other = hold(api, [['room-a']], day(3, 14), day(3, 16)).data
    # Into a taken time: refused, and nothing moves.
    r = api.post(f'/v1/bookings/{b["id"]}/reschedule/', {'start': iso(day(3, 15)), 'end': iso(day(3, 17))})
    assert r.status_code == 409
    assert api.get(f'/v1/bookings/{b["id"]}/').data['start'] == b['start']
    assert hold(api, [['room-a']], day(3, 10), day(3, 12)).status_code == 409  # old time still held
    # Into a free time: moves, old time freed, same engineer kept.
    r = api.post(f'/v1/bookings/{b["id"]}/reschedule/', {'start': iso(day(3, 17)), 'end': iso(day(3, 19))})
    assert r.status_code == 200, r.data
    assert [x['ref'] for x in r.data['resources']] == ['room-a', 'eng-1']
    assert hold(api, [['room-a']], day(3, 10), day(3, 12)).status_code == 201
    assert other['status'] == 'held'


# --- blocks ------------------------------------------------------------------

def test_a_block_cannot_land_on_a_booking_and_blocks_new_ones(api):
    studio(api)
    b = hold(api, [['room-a']], day(3, 10), day(3, 12), external_ref='B1').data
    r = api.post('/v1/blocks/', {'resource': 'room-a', 'start': iso(day(3, 11)), 'end': iso(day(3, 15))})
    assert r.status_code == 409
    assert r.data['details']['conflicts'] == [{'booking_id': b['id'], 'external_ref': 'B1'}]
    r = api.post('/v1/blocks/', {'resource': 'room-a', 'start': iso(day(3, 12)), 'end': iso(day(3, 15)),
                                 'reason': 'Fixing the desk'})
    assert r.status_code == 201
    assert hold(api, [['room-a']], day(3, 13), day(3, 14)).status_code == 409
    assert api.delete(f'/v1/blocks/{r.data["id"]}/').status_code == 204
    assert hold(api, [['room-a']], day(3, 13), day(3, 14)).status_code == 201


def test_closure_lists_bookings_inside_it(api):
    studio(api)
    b = hold(api, [['room-a']], day(3, 10), day(3, 12)).data
    r = api.post('/v1/closures/', {'start': iso(day(3)), 'end': iso(day(4)), 'reason': 'Holiday'})
    assert r.data['affected_bookings'] == [{'id': b['id'], 'external_ref': ''}]


# --- idempotency and webhooks ------------------------------------------------

def test_idempotent_retry_returns_the_first_answer(api):
    studio(api)
    body = {'requirements': [{'resources': ['room-a']}], 'start': iso(day(3, 10)), 'end': iso(day(3, 12))}
    a = api.post('/v1/bookings/', body, HTTP_IDEMPOTENCY_KEY='k-1')
    b = api.post('/v1/bookings/', body, HTTP_IDEMPOTENCY_KEY='k-1')
    assert (a.status_code, b.status_code) == (201, 201)
    assert a.data['id'] == b.data['id']
    assert b.headers['Idempotent-Replayed'] == 'true'
    assert Booking.objects.count() == 1
    other = {**body, 'start': iso(day(3, 14)), 'end': iso(day(3, 15))}
    r = api.post('/v1/bookings/', other, HTTP_IDEMPOTENCY_KEY='k-1')
    assert (r.status_code, r.data['code']) == (422, 'idempotency_key_reused')


def test_webhooks_are_signed(api, ona, webhook_calls):
    studio(api)
    hold(api, [['room-a']], day(3, 10), day(3, 12))
    call = webhook_calls[0]
    t, v1 = (p.split('=', 1)[1] for p in call['headers']['Booking-Signature'].split(','))
    assert v1 == webhooks.sign(ona.secret, int(t), call['body'])
    assert json.loads(call['body'])['data']['resources'] == [{'ref': 'room-a', 'kind': 'room', 'group': 0}]
