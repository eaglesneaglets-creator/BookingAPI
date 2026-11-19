"""Each platform's bookings are its own: never visible, bookable or blocked by another platform."""

from rest_framework.test import APIClient

from booking.models import ApiKey
from conftest import add_resource, day, hold


def test_the_same_ref_on_two_platforms_is_two_unrelated_resources(api, eagles_api):
    add_resource(api, 'room-a')
    add_resource(eagles_api, 'room-a')
    start, end = day(3, 10), day(3, 12)
    assert hold(api, [['room-a']], start, end).status_code == 201
    # ONA holding its room-a at 10:00 does not touch E&E's room-a.
    assert hold(eagles_api, [['room-a']], start, end).status_code == 201


def test_a_platform_cannot_see_or_change_another_platforms_booking(api, eagles_api):
    add_resource(api, 'room-a')
    b = hold(api, [['room-a']], day(3, 10), day(3, 12)).data
    for call in (lambda: eagles_api.get(f'/v1/bookings/{b["id"]}/'),
                 lambda: eagles_api.post(f'/v1/bookings/{b["id"]}/confirm/'),
                 lambda: eagles_api.post(f'/v1/bookings/{b["id"]}/cancel/', {}),
                 lambda: eagles_api.post(f'/v1/bookings/{b["id"]}/extend/', {'minutes': 30})):
        assert call().status_code == 404
    assert eagles_api.get('/v1/bookings/').data['count'] == 0
    assert api.get(f'/v1/bookings/{b["id"]}/').data['status'] == 'held'


def test_a_platform_cannot_name_another_platforms_resources(api, eagles_api):
    add_resource(api, 'ona-room')
    r = hold(eagles_api, [['ona-room']], day(3, 10), day(3, 12))
    assert r.status_code == 400
    assert eagles_api.get('/v1/resources/ona-room/').status_code == 404
    assert eagles_api.get('/v1/resources/').data['count'] == 0
    r = eagles_api.post('/v1/blocks/', {'resource': 'ona-room', 'start': '2030-01-01T10:00:00Z',
                                         'end': '2030-01-01T11:00:00Z'})
    assert r.status_code == 404


def test_kind_groups_only_reach_the_callers_own_resources(api, eagles_api):
    add_resource(api, 'ona-eng', kind='engineer')
    add_resource(eagles_api, 'mentor-1', kind='engineer')
    r = hold(eagles_api, [{'kind': 'engineer', 'required': 1}], day(3, 10), day(3, 11))
    assert r.status_code == 201
    assert [x['ref'] for x in r.data['resources']] == ['mentor-1']


def test_closures_only_close_their_own_platform(api, eagles_api):
    add_resource(api, 'room-a')
    add_resource(eagles_api, 'room-a')
    r = api.post('/v1/closures/', {'start': '2030-01-01T00:00:00Z', 'end': '2030-01-02T00:00:00Z'})
    assert r.status_code == 201
    assert eagles_api.get('/v1/closures/').data['count'] == 0


def test_keys(api, ona, db):
    assert APIClient().get('/v1/bookings/').status_code in (401, 403)
    bad = APIClient()
    bad.credentials(HTTP_AUTHORIZATION='Bearer bk_nope_nope')
    assert bad.get('/v1/bookings/').status_code == 401
    key, raw = ApiKey.issue(ona, 'to revoke')
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {raw}')
    assert c.get('/v1/bookings/').status_code == 200
    from django.utils import timezone
    key.revoked_at = timezone.now()
    key.save()
    assert c.get('/v1/bookings/').status_code == 401


def test_closures_by_ref_are_per_platform(api, eagles_api):
    add_resource(api, 'room-a')
    add_resource(eagles_api, 'room-a')
    body = {'start': '2030-01-01T00:00:00Z', 'end': '2030-01-02T00:00:00Z', 'kind': 'holiday'}
    assert api.put('/v1/closures/ref/x/', body).status_code == 201
    assert eagles_api.put('/v1/closures/ref/x/', body).status_code == 201   # its own "x"
    assert eagles_api.delete('/v1/closures/ref/x/').status_code == 204
    assert api.get('/v1/closures/').data['count'] == 1                       # ONA's survives
    r = eagles_api.put('/v1/closures/ref/y/', {**body, 'resources': ['ona-only']})
    assert r.status_code == 400
