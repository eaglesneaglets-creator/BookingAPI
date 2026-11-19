from datetime import date

from conftest import add_resource, day, hold, iso


def search(api, groups, start, end, **extra):
    reqs = [g if isinstance(g, dict) else {'resources': g} for g in groups]
    r = api.post('/v1/availability/', {'requirements': reqs, 'start': start, 'end': end, **extra})
    assert r.status_code == 200, r.data
    return r.data['slots']


def test_free_slots_skip_booked_time(api):
    add_resource(api, 'room-a')
    hold(api, [['room-a']], day(3, 10), day(3, 12))
    d = day(3).date().isoformat()
    slots = search(api, [['room-a']], d, d, duration_minutes=60, step_minutes=60)
    starts = [s['start'][11:16] for s in slots]
    assert '09:00' in starts and '12:00' in starts
    assert '10:00' not in starts and '11:00' not in starts
    assert len(slots) == 22


def test_reasons_when_asked(api):
    add_resource(api, 'room-a', hours={'weekly': [{'weekday': w, 'start': '08:00', 'end': '20:00'}
                                                  for w in range(7)]})
    hold(api, [['room-a']], day(3, 10), day(3, 11))
    api.post('/v1/blocks/', {'resource': 'room-a', 'start': iso(day(3, 12)), 'end': iso(day(3, 13))})
    api.post('/v1/closures/', {'start': iso(day(3, 15)), 'end': iso(day(3, 16))})
    d = day(3).date().isoformat()
    slots = search(api, [['room-a']], d, d, duration_minutes=60, step_minutes=60, include_unavailable=True)
    by_hour = {s['start'][11:16]: s.get('reason', 'free') for s in slots}
    assert by_hour['07:00'] == 'outside_hours'
    assert by_hour['09:00'] == 'free'
    assert by_hour['10:00'] == 'booked'
    assert by_hour['12:00'] == 'blocked'
    assert by_hour['15:00'] == 'closed'


def test_any_one_of_lists_who_is_free(api):
    add_resource(api, 'room-a')
    for e in ('eng-1', 'eng-2'):
        add_resource(api, e, kind='engineer')
    hold(api, [['eng-1']], day(3, 10), day(3, 11))
    slots = search(api, [['room-a'], {'kind': 'engineer', 'required': 1}],
                   iso(day(3, 10)), iso(day(3, 11)), duration_minutes=60)
    assert slots[0]['free'] == [['room-a'], ['eng-2']]
    hold(api, [['eng-2']], day(3, 10), day(3, 11))
    assert search(api, [['room-a'], {'kind': 'engineer', 'required': 1}],
                  iso(day(3, 10)), iso(day(3, 11)), duration_minutes=60) == []


def test_an_overdue_hold_shows_as_free(api):
    from booking.models import Booking
    from django.utils import timezone
    add_resource(api, 'room-a')
    b = hold(api, [['room-a']], day(3, 10), day(3, 11)).data
    Booking.objects.filter(pk=b['id']).update(hold_expires_at=timezone.now())
    assert len(search(api, [['room-a']], iso(day(3, 10)), iso(day(3, 11)), duration_minutes=60)) == 1


def test_opening_hours_follow_daylight_saving(api):
    """A London mentor opening at 09:00 is 09:00 UTC in winter and 08:00 UTC in summer."""
    add_resource(api, 'mentor-ldn', kind='mentor', timezone='Europe/London', max_advance_days=5 * 365,
                 hours={'weekly': [{'weekday': w, 'start': '09:00', 'end': '10:00'} for w in range(7)]})
    for d, utc_hour in ((date(2030, 3, 29), '09:00'), (date(2030, 4, 1), '08:00')):
        slots = search(api, [['mentor-ldn']], d.isoformat(), d.isoformat(), duration_minutes=60,
                       timezone='UTC')
        assert [s['start'][11:16] for s in slots] == [utc_hour], d


def test_date_override_closes_a_day(api):
    add_resource(api, 'room-a')
    d = day(5).date().isoformat()
    assert api.put(f'/v1/resources/room-a/overrides/{d}/', {'hours': [], 'reason': 'Deep clean'}).status_code == 200
    assert search(api, [['room-a']], d, d, duration_minutes=60) == []
    api.delete(f'/v1/resources/room-a/overrides/{d}/')
    assert len(search(api, [['room-a']], d, d, duration_minutes=60, step_minutes=60)) == 24


def test_search_window_is_capped(api):
    add_resource(api, 'room-a')
    r = api.post('/v1/availability/', {'requirements': [{'resources': ['room-a']}], 'start': '2030-01-01',
                                       'end': '2030-06-01', 'duration_minutes': 60})
    assert r.status_code == 400
