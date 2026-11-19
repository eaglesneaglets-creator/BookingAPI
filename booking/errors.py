"""
Errors a platform can act on.

Every error body has a stable `code` a platform can branch on, a sentence for
people, and sometimes `details`:

    409 {"code": "slot_unavailable", "detail": "…", "details": {"groups": [...]}}
"""

from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler as drf_handler


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = 'conflict'
    default_detail = 'That conflicts with the current state.'

    def __init__(self, detail=None, code=None, details=None):
        super().__init__(detail, code)
        self.details = details


class SlotUnavailable(Conflict):
    default_code = 'slot_unavailable'
    default_detail = 'That time is not available.'


class SlotTaken(Conflict):
    """The hold ran out and someone else has the time now (e.g. payment arrived late)."""

    default_code = 'slot_taken'
    default_detail = 'The hold ran out and that time has been taken since.'


class WrongStatus(Conflict):
    default_code = 'wrong_status'


class IdempotencyMismatch(APIException):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = 'idempotency_key_reused'
    default_detail = 'This Idempotency-Key was already used for a different request.'


def exception_handler(exc, context):
    response = drf_handler(exc, context)
    if response is None:
        return None
    if isinstance(exc, APIException) and isinstance(response.data, dict) and 'detail' in response.data:
        codes = exc.get_codes()
        response.data['code'] = codes if isinstance(codes, str) else exc.default_code
        if getattr(exc, 'details', None) is not None:
            response.data['details'] = exc.details
    elif isinstance(exc, APIException):
        response.data = {'code': 'invalid', 'detail': 'Some fields are not valid.', 'fields': response.data}
    return response
