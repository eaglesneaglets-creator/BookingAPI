"""
End-to-end test of the Booking API in Docker.

Builds the real image, starts it with STAGING settings next to Postgres,
Redis, the Celery worker (running hold expiry) and a pretend platform, then
drives the API the way ONA and Eagles & Eaglets would and checks every result.

    .venv/Scripts/python e2e/run.py            # build, test, tear down
    .venv/Scripts/python e2e/run.py --keep     # leave the stack running after

Exit code 0 only if every check passed.
"""

import hashlib
import hmac
import json
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ['docker', 'compose', '-f', str(ROOT / 'e2e' / 'docker-compose.yml')]
API = 'http://localhost:8120'
HOOK = 'http://localhost:9022'
# Staging redirects plain HTTP to HTTPS; behind Railway's proxy this header
# says the original request was HTTPS. Here we say it ourselves.
PROXY = {'X-Forwarded-Proto': 'https'}

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail='') -> bool:
    results.append((name, bool(ok), str(detail)))
    print(f'  {"PASS" if ok else "FAIL"}  {name}' + (f'  — {str(detail)[:600]}' if detail and not ok else ''))
    return ok


def sh(*args, input_text=None, timeout=600) -> subprocess.CompletedProcess:
    return subprocess.run([*COMPOSE, *args], input=input_text, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout, cwd=ROOT)


def django(code: str) -> dict:
    """Run Python in the web container's Django shell; it prints RESULT:<json>."""
    r = sh('exec', '-T', 'web', 'python', 'manage.py', 'shell', input_text=code)
    for line in r.stdout.splitlines():
        if line.startswith('RESULT:'):
            return json.loads(line[len('RESULT:'):])
    raise RuntimeError(f'Django shell gave no result.\nstdout: {r.stdout[-1500:]}\nstderr: {r.stderr[-1500:]}')


def client(key: str):
    s = requests.Session()
    s.headers.update({**PROXY, 'Authorization': f'Bearer {key}'})
    return s


def received() -> list[dict]:
    return requests.get(f'{HOOK}/received', timeout=5).json()


def wait_for(fn, seconds: float, every: float = 2.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(every)
    return fn()


def signature_ok(msg: dict, secret: str) -> bool:
    parts = dict(p.split('=', 1) for p in msg['signature'].split(','))
    expected = hmac.new(secret.encode(), f'{parts["t"]}.'.encode() + msg['body'].encode(),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts['v1']) and abs(time.time() - int(parts['t'])) < 300


def events(event_type: str, booking_id: str) -> list[dict]:
    out = []
    for m in received():
        body = json.loads(m['body'])
        if body['type'] == event_type and body['data']['booking_id'] == booking_id:
            out.append(m)
    return out


def at(days: int, hour: int) -> str:
    base = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return (base + timedelta(days=days, hours=hour)).isoformat().replace('+00:00', 'Z')


def bring_up() -> None:
    print('== Building the image and starting the stack (first build takes a few minutes)')
    r = sh('up', '--build', '-d', timeout=1800)
    if r.returncode != 0:
        print(r.stdout[-3000:], r.stderr[-3000:])
        sys.exit('Stack failed to start.')

    def healthy():
        try:
            return requests.get(f'{API}/health/', timeout=3).status_code == 200
        except requests.RequestException:
            return False

    if not wait_for(healthy, 180):
        print(sh('logs', '--tail', '80', 'web').stdout)
        sys.exit('Web service never became healthy.')


def setup() -> dict:
    print('== Two platforms and their keys')
    r = sh('exec', '-T', 'web', 'python', 'manage.py', 'create_platform', 'ona-records', '--name', 'ONA Records',
           '--webhook-url', 'https://hook.invalid/booking', '--key-name', 'ONA backend')
    out = dict(line.split('=', 1) for line in r.stdout.splitlines() if line.startswith('BOOKING_'))
    check('create_platform prints a key and a webhook secret once',
          out.get('BOOKING_API_KEY', '').startswith('bk_') and out.get('BOOKING_WEBHOOK_SECRET', '').startswith('whsec_'),
          r.stdout + r.stderr)
    r = sh('exec', '-T', 'web', 'python', 'manage.py', 'create_platform', 'ona-records', '--name', 'ONA Records',
           '--webhook-url', 'http://hook:9000/ona')
    check('create_platform refuses a plain-http webhook URL on staging', r.returncode != 0)
    rest = django("""
import json
from booking.models import ApiKey, Platform
ona = Platform.objects.get(slug='ona-records')
ona.webhook_url = 'http://hook:9000/ona'
ona.save()
eagles = Platform.objects.create(slug='eagles', name='Eagles & Eaglets')
_, key = ApiKey.issue(eagles, 'Eagles backend')
print('RESULT:' + json.dumps({'eagles': key}))
""")
    return {'ona': out['BOOKING_API_KEY'], 'secret': out['BOOKING_WEBHOOK_SECRET'], **rest}


ALWAYS = {'weekly': [{'weekday': d, 'start': '00:00', 'end': '24:00'} for d in range(7)]}
OFFICE = {'weekly': [{'weekday': d, 'start': '09:00', 'end': '17:00'} for d in range(5)]}


def scenario(keys: dict) -> None:
    ona, eagles, secret = client(keys['ona']), client(keys['eagles']), keys['secret']

    print('== ONA sets up a studio; Eagles sets up a mentor (and its own "room-a")')
    for ref, kind in (('room-a', 'room'), ('eng-1', 'engineer'), ('eng-2', 'engineer')):
        ona.put(f'{API}/v1/resources/{ref}/', json={'kind': kind, 'name': ref})
        ona.put(f'{API}/v1/resources/{ref}/schedule/', json=ALWAYS)
    eagles.put(f'{API}/v1/resources/mentor-1/', json={'kind': 'mentor', 'name': 'Mentor 1',
                                                      'buffer_after_minutes': 10})
    eagles.put(f'{API}/v1/resources/mentor-1/schedule/', json=OFFICE)
    eagles.put(f'{API}/v1/resources/room-a/', json={'kind': 'room', 'name': 'E&E room'})
    eagles.put(f'{API}/v1/resources/room-a/schedule/', json=ALWAYS)
    check('each platform sees only its own resources',
          ona.get(f'{API}/v1/resources/').json()['count'] == 3
          and eagles.get(f'{API}/v1/resources/').json()['count'] == 2)

    print('== Availability')
    r = ona.post(f'{API}/v1/availability/', json={
        'requirements': [{'resources': ['room-a']}, {'kind': 'engineer', 'required': 1}],
        'start': at(3, 0)[:10], 'end': at(3, 0)[:10], 'duration_minutes': 120, 'step_minutes': 60})
    check('a 24/7 studio offers every 2-hour slot of a day', r.status_code == 200 and len(r.json()['slots']) == 23,
          r.text[:300])

    print('== 25 customers press "book" on the same slot at the same instant')
    body = {'requirements': [{'resources': ['room-a']}, {'kind': 'engineer', 'required': 1}],
            'start': at(3, 10), 'end': at(3, 13), 'hold_minutes': 30}
    with ThreadPoolExecutor(25) as pool:
        responses = list(pool.map(lambda _: client(keys['ona']).post(f'{API}/v1/bookings/', json=body), range(25)))
    codes = Counter(r.status_code for r in responses)
    check('exactly one hold wins, the other 24 get 409', codes == {201: 1, 409: 24}, codes)
    winner = next(r.json() for r in responses if r.status_code == 201)

    with ThreadPoolExecutor(10) as pool:
        any_eng = list(pool.map(lambda _: client(keys['ona']).post(f'{API}/v1/bookings/', json={
            'requirements': [{'kind': 'engineer', 'required': 1}], 'start': at(4, 10), 'end': at(4, 11)}),
            range(10)))
    won = sorted(r.json()['resources'][0]['ref'] for r in any_eng if r.status_code == 201)
    check('10 customers, "any engineer", 2 engineers: two win, one each', won == ['eng-1', 'eng-2'], won)

    print('== Platforms stay apart')
    r = eagles.post(f'{API}/v1/bookings/', json={'requirements': [{'resources': ['room-a']}],
                                                 'start': at(3, 10), 'end': at(3, 13)})
    check("Eagles books ITS room-a at the time ONA's room-a is held", r.status_code == 201, r.text[:300])
    check("Eagles cannot see ONA's booking", eagles.get(f'{API}/v1/bookings/{winner["id"]}/').status_code == 404)
    check("Eagles cannot cancel ONA's booking",
          eagles.post(f'{API}/v1/bookings/{winner["id"]}/cancel/', json={}).status_code == 404)

    print('== Mentor rules: office hours and the 10-minute buffer')
    r = eagles.post(f'{API}/v1/bookings/', json={'requirements': [{'resources': ['mentor-1']}],
                                                 'start': at(7, 18), 'end': at(7, 19)})
    check('a mentor session out of office hours is refused', r.status_code == 409, r.text[:300])

    print('== Payment, idempotency and webhooks')
    r = ona.post(f'{API}/v1/bookings/{winner["id"]}/confirm/', headers={'Idempotency-Key': 'pay-1'})
    r2 = ona.post(f'{API}/v1/bookings/{winner["id"]}/confirm/', headers={'Idempotency-Key': 'pay-1'})
    check('confirm, and its retry replays the same answer',
          r.json().get('status') == 'confirmed' and r2.headers.get('Idempotent-Replayed') == 'true')
    held = wait_for(lambda: events('booking.held', winner['id']), 20)
    confirmed = wait_for(lambda: events('booking.confirmed', winner['id']), 20)
    check('webhooks booking.held and booking.confirmed arrive, correctly signed',
          held and confirmed and signature_ok(held[0], secret) and signature_ok(confirmed[0], secret))

    print('== A hold that nobody pays for runs out on its own (the worker sweeps every minute)')
    r = ona.post(f'{API}/v1/bookings/', json={'requirements': [{'resources': ['eng-2']}], 'start': at(5, 10),
                                              'end': at(5, 11), 'hold_minutes': 1})
    lapsing = r.json()
    status = wait_for(lambda: ona.get(f'{API}/v1/bookings/{lapsing["id"]}/').json()['status'] == 'expired', 150, 5)
    check('the 1-minute hold is expired by the worker within 2.5 minutes', status)
    expired = wait_for(lambda: events('booking.expired', lapsing['id']), 30)
    check('webhook booking.expired arrives, correctly signed', expired and signature_ok(expired[0], secret))
    r = ona.post(f'{API}/v1/bookings/{lapsing["id"]}/confirm/')
    check('late payment: the expired hold is confirmed because its time is still free',
          r.status_code == 200 and r.json()['status'] == 'confirmed', r.text[:300])


def unit_tests_on_postgres() -> None:
    print('== The unit test suite, inside the image, on Postgres')
    r = sh('exec', '-T', '-e', 'DJANGO_SETTINGS_MODULE=config.settings.local', 'web',
           'python', '-m', 'pytest', '-p', 'no:cacheprovider', '-p', 'no:warnings', '-q', timeout=900)
    last = (r.stdout.strip().splitlines() or [''])[-1]
    check(f'pytest on Postgres: {last}', r.returncode == 0, r.stdout[-2500:] + r.stderr[-1500:])


def main() -> int:
    keep = '--keep' in sys.argv
    bring_up()
    try:
        keys = setup()
        scenario(keys)
        unit_tests_on_postgres()
        logs = sh('logs', 'web', 'worker').stdout
        check('no tracebacks in web or worker logs', 'Traceback' not in logs,
              logs[logs.find('Traceback') - 200: logs.find('Traceback') + 1500] if 'Traceback' in logs else '')
    except Exception as exc:  # report, then still tear down
        check('scenario ran to the end', False, repr(exc))
    finally:
        if not keep:
            sh('down', '-v', '--remove-orphans')
    failed = [r for r in results if not r[1]]
    print(f'\n{len(results) - len(failed)}/{len(results)} checks passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
