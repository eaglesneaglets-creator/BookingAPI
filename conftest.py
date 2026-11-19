from datetime import UTC, datetime, timedelta

import pytest
from rest_framework.test import APIClient

from booking.models import ApiKey, Platform
from booking.webhooks import new_secret


@pytest.fixture(autouse=True)
def _inline(monkeypatch, settings):
    settings.CELERY_BROKER_URL = ''
    # Tests run inside a transaction that is rolled back, so commits never
    # happen; run after-commit work (webhooks) at once.
    from django.db import transaction
    monkeypatch.setattr(transaction, 'on_commit', lambda fn, using=None, robust=False: fn())


def client_for(platform: Platform) -> APIClient:
    _, raw = ApiKey.issue(platform, 'tests')
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {raw}')
    return c


@pytest.fixture
def ona(db):
    p = Platform.objects.create(slug='ona-records', name='ONA Records', webhook_url='https://ona.test/booking/hook/')
    p.secret = new_secret(p)
    return p


@pytest.fixture
def eagles(db):
    return Platform.objects.create(slug='eagles', name='Eagles & Eaglets')


@pytest.fixture
def api(ona):
    return client_for(ona)


@pytest.fixture
def eagles_api(eagles):
    return client_for(eagles)


@pytest.fixture
def webhook_calls(monkeypatch):
    """Capture outgoing webhook POSTs; answer 200."""
    import booking.webhooks as wh

    calls = []

    class Resp:
        status_code = 200

    def fake_post(url, data, headers, timeout, allow_redirects):
        calls.append({'url': url, 'body': data, 'headers': headers})
        return Resp()

    monkeypatch.setattr(wh.requests, 'post', fake_post)
    return calls


ALWAYS_OPEN = {'weekly': [{'weekday': d, 'start': '00:00', 'end': '24:00'} for d in range(7)]}


def add_resource(api, ref, kind='room', *, hours=ALWAYS_OPEN, **fields):
    r = api.put(f'/v1/resources/{ref}/', {'kind': kind, 'name': ref.replace('-', ' ').title(), **fields})
    assert r.status_code in (200, 201), r.data
    if hours is not None:
        r = api.put(f'/v1/resources/{ref}/schedule/', hours)
        assert r.status_code == 200, r.data
    return ref


def day(n: int, hour: int = 0, minute: int = 0) -> datetime:
    """`n` days from today at hour:minute UTC (= Accra time)."""
    base = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return base + timedelta(days=n, hours=hour, minutes=minute)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace('+00:00', 'Z')


def hold(api, groups, start, end, **extra):
    """groups: [['room-a'], {'kind': 'engineer', 'required': 1}, …]"""
    reqs = [g if isinstance(g, dict) else {'resources': g} for g in groups]
    return api.post('/v1/bookings/', {'requirements': reqs, 'start': iso(start), 'end': iso(end), **extra})
