# Booking API

Time as a service. Any platform (ONA Records, Eagles & Eaglets, and the next
one) connects with an API key, describes what can be booked, and asks one
question: **is this time free, and if so, hold it.** The engine owns time;
each platform keeps its own prices, payments, customers and invoices.

Every platform's resources and bookings are its own. One platform can never
see, book, block or cancel another's, and the same ref (`room-a`) on two
platforms is two unrelated resources.

## How a platform uses it

```
Platform backend                               Booking API
────────────────                               ───────────
PUT  /v1/resources/{ref}/            ───────>  a room, an engineer, a mentor…
PUT  /v1/resources/{ref}/schedule/   ───────>  weekly hours + date overrides
POST /v1/availability/               ───────>  free slots for "room-a AND any engineer"
POST /v1/bookings/                   ───────>  hold (expires unless confirmed)
                                     <───────  webhook  booking.held
     (customer pays on the platform)
POST /v1/bookings/{id}/confirm/      ───────>  confirmed
                                     <───────  webhook  booking.confirmed
         … or nobody pays …
                                     <───────  webhook  booking.expired   (time released)
```

Every call sends `Authorization: Bearer bk_<prefix>_<secret>` from the
platform's backend; the browser never talks to this service.

Interactive docs: `/docs/` (signed-in admin staff), schema at `/schema/`.

### Requirements: what a booking needs

```json
"requirements": [
  { "resources": ["room-a"] },                       // this room ("required": "all" by default)
  { "kind": "engineer", "required": 1 }              // and any one engineer
]
```

A booking takes every group or nothing. When a group says "any N", the engine
picks who and says which (`resources` in the answer): the least booked on
that local day first (minutes of held and confirmed bookings), ties in the
order you sent, so work is spread evenly across engineers or mentors.

### The calls

| Call | Does |
|---|---|
| `GET /v1/resources/?kind=` · `GET/PUT /v1/resources/{ref}/` | Your resources: kind, name, time zone, buffers, notice, how far ahead |
| `GET/PUT /v1/resources/{ref}/schedule/` | Weekly hours (`"24:00"` = end of day) and date overrides |
| `PUT/DELETE /v1/resources/{ref}/overrides/{YYYY-MM-DD}/` | One day's hours (`"hours": []` = closed) |
| `POST /v1/availability/` | Slots for requirements over up to 62 days; `include_unavailable` adds each slot's reason |
| `POST /v1/bookings/` | Hold (`hold_minutes`), or `confirm: true` to book outright; `override_rules` for staff |
| `GET /v1/bookings/?resource=&status=&from=&to=&external_ref=&subject_ref=` | Your bookings |
| `POST /v1/bookings/{id}/confirm/` · `extend/` · `cancel/` · `reschedule/` | Lifecycle |
| `GET/POST /v1/blocks/` · `DELETE /v1/blocks/{id}/` | Maintenance / time off on one resource |
| `GET/POST /v1/closures/` · `DELETE /v1/closures/{id}/` | Holidays: the whole platform, or some kinds |

Errors carry a stable `code`: `slot_unavailable` (with which group failed and
why, per resource: `booked`, `blocked`, `closed`, `outside_hours`,
`too_soon`, `too_far`, `inactive`), `slot_taken`, `wrong_status`,
`hold_expired`, `duplicate_external_ref`, `idempotency_key_reused`.

### Holds and payment

- A hold lasts `hold_minutes` (default 15, at most 7 days). Choose it per call:
  ONA passes its "hours to hold an unpaid booking" setting.
- Payment still in progress? `POST …/extend/ {"minutes": 30}`.
- Payment arrived after the hold ran out? `confirm` anyway: the booking is
  confirmed if its time is still free, or you get 409 `slot_taken` and refund.
- Send `Idempotency-Key: <your id>` on writes. A retry with the same key
  returns the first answer; it never books twice, even when both arrive at once.

### Webhooks

`booking.held`, `booking.confirmed`, `booking.rescheduled`,
`booking.cancelled`, `booking.expired`, POSTed to the platform's URL with:

    Booking-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>." + raw body)>
    Booking-Event-Id:  <uuid>   (ignore one you have handled; retries reuse it)

Reject a `t` more than 5 minutes old. Failed deliveries are retried for about
a day and a half.

## Why two customers can never get the same hour

1. **The database decides.** Taking time inserts an active `Allocation`, and
   a Postgres exclusion constraint refuses an overlapping one on the same
   resource, whatever code path or how many servers. No check-then-write gap.
2. **All or nothing.** A booking's room and engineer are inserted in one
   transaction.
3. **No deadlocks.** Each write first takes a lock per resource it may touch,
   in one global order, so two bookings wanting the same room and engineer
   in opposite orders queue instead of deadlocking.
4. **Row locks on every change**: confirm, cancel and the expiry sweeper can't
   interleave on one booking.
5. **Expiry can't strand a slot.** The worker expires overdue holds every
   minute, and anyone asking for an overdue hold's time expires it first.

`booking/tests/test_concurrency.py` proves it with real threads on Postgres:
30 customers for one slot (one wins), overlapping times, opposite orders,
"any engineer" with 3 engineers and 10 customers, one idempotency key sent 10
times at once, confirm racing the sweeper, and late payment racing a new
customer.

## Run it locally

```bash
docker compose up -d db                     # Postgres on 5434
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python manage.py migrate
.venv/Scripts/python -m pytest              # unit + race tests
.venv/Scripts/python e2e/run.py             # full stack in Docker, staging settings
.venv/Scripts/python manage.py create_platform ona-records --name "ONA Records" \
    --webhook-url https://<ona-backend>/api/v1/booking/webhook/ --key-name "ONA staging"
```

## Deploy (Railway, like the KYC API)

Own project: **Booking-API** (web, `./start.sh`) + **Worker** (`./start-worker.sh`,
`CELERY_BEAT=1`) + Postgres + Redis. Variables are listed in `.env.example`.
Health check path: `/health/`. Then run `create_platform` once per platform
and put the printed key and webhook secret into that platform's variables.
