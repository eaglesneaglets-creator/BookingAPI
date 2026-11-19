"""
Idempotency-Key: a retried write returns the first answer instead of acting twice.

    POST /v1/bookings/   Idempotency-Key: 7f3a…   → 201 (booking made)
    POST /v1/bookings/   Idempotency-Key: 7f3a…   → 201, the same booking, header Idempotent-Replayed: true

The record is inserted in the SAME transaction as the work. If two requests
with one key arrive together, the second's insert waits on the unique index
until the first commits, then fails and replays the stored answer; so a key
can never do its work twice, even at the same instant. If the work fails
with an exception the record rolls back with it, and a retry runs afresh.
Keys are per platform. Reusing a key for a different request is a 422.
"""

import hashlib
import json

from django.db import IntegrityError, transaction
from rest_framework.response import Response

from .errors import IdempotencyMismatch
from .models import IdempotencyRecord

HEADER = 'Idempotency-Key'


def _hash(request) -> str:
    body = json.dumps(request.data, sort_keys=True, default=str)
    return hashlib.sha256(f'{request.method} {request.path}\n{body}'.encode()).hexdigest()


def run(request, work) -> Response:
    key = request.headers.get(HEADER, '').strip()
    if not key:
        return work()
    if len(key) > 200:
        raise IdempotencyMismatch('Idempotency-Key is at most 200 characters.', 'invalid_idempotency_key')
    platform, digest = request.user.platform, _hash(request)
    try:
        with transaction.atomic():
            record = IdempotencyRecord.objects.create(platform=platform, key=key, request_hash=digest)
            response = work()
            record.status_code, record.body = response.status_code, response.data
            record.save(update_fields=['status_code', 'body'])
            return response
    except IntegrityError:
        record = IdempotencyRecord.objects.filter(platform=platform, key=key).first()
        if record is None:  # not our unique index: a real error from the work
            raise
    if record.request_hash != digest:
        raise IdempotencyMismatch()
    return Response(record.body, status=record.status_code, headers={'Idempotent-Replayed': 'true'})
