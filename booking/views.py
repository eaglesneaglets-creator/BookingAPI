"""
The API. Every view works on `request.user.platform` and nothing else: a
platform's key can only ever see and change that platform's rows.
"""

from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from . import availability, holds, idempotency
from .models import Allocation, Block, Booking, BookingStatus, Closure, DateOverride, Resource, WeeklyHours
from .serializers import (
    AvailabilitySerializer,
    AwareDateTimeField,
    BlockCreateSerializer,
    BlockSerializer,
    BookingCreateSerializer,
    BookingSerializer,
    CancelSerializer,
    ClosureCreateSerializer,
    ClosureSerializer,
    ExtendSerializer,
    OverrideSerializer,
    RescheduleSerializer,
    ResourceSerializer,
    ScheduleSerializer,
    resolve_requirements,
    schedule_out,
    window,
)

IDEMPOTENCY = OpenApiParameter('Idempotency-Key', str, OpenApiParameter.HEADER, required=False,
                               description='Retry safely: the same key returns the first answer.')


def platform_of(request):
    return request.user.platform


def bookings_qs(platform):
    return (Booking.objects.filter(platform=platform)
            .prefetch_related(Prefetch('allocations', queryset=Allocation.objects.select_related('resource'))))


def booking_response(platform, b: Booking, code=status.HTTP_200_OK) -> Response:
    return Response(BookingSerializer(bookings_qs(platform).get(pk=b.pk)).data, status=code)


def parse_instant(request, name):
    value = request.query_params.get(name)
    if not value:
        return None
    try:
        return AwareDateTimeField().to_internal_value(value)
    except serializers.ValidationError as exc:
        raise serializers.ValidationError({name: exc.detail}) from exc


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

class ResourceList(APIView):
    @extend_schema(parameters=[OpenApiParameter('kind', str)], responses=ResourceSerializer(many=True))
    def get(self, request):
        qs = Resource.objects.filter(platform=platform_of(request))
        if kind := request.query_params.get('kind'):
            qs = qs.filter(kind=kind)
        page = PageNumberPagination()
        return page.get_paginated_response(ResourceSerializer(page.paginate_queryset(qs, request, self), many=True).data)


class ResourceDetail(APIView):
    @extend_schema(responses=ResourceSerializer)
    def get(self, request, ref):
        return Response(ResourceSerializer(get_object_or_404(Resource, platform=platform_of(request),
                                                             external_ref=ref)).data)

    @extend_schema(request=ResourceSerializer, responses=ResourceSerializer,
                   description='Create or update the resource with your ref. Changing buffers affects new '
                               'bookings only.')
    def put(self, request, ref):
        platform = platform_of(request)
        existing = Resource.objects.filter(platform=platform, external_ref=ref).first()
        s = ResourceSerializer(existing, data=request.data, partial=existing is not None)
        s.is_valid(raise_exception=True)
        if len(ref) > 120:
            raise serializers.ValidationError({'ref': 'At most 120 characters.'})
        r = s.save(platform=platform, external_ref=ref)
        return Response(ResourceSerializer(r).data, status=status.HTTP_200_OK if existing else status.HTTP_201_CREATED)


class ScheduleView(APIView):
    def _resource(self, request, ref):
        return get_object_or_404(Resource.objects.prefetch_related('weekly_hours', 'overrides'),
                                 platform=platform_of(request), external_ref=ref)

    @extend_schema(responses=ScheduleSerializer)
    def get(self, request, ref):
        return Response(schedule_out(self._resource(request, ref)))

    @extend_schema(request=ScheduleSerializer, responses=ScheduleSerializer,
                   description='Replace the weekly hours and every date override.')
    def put(self, request, ref):
        r = self._resource(request, ref)
        s = ScheduleSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        with transaction.atomic():
            Resource.objects.select_for_update().filter(pk=r.pk).first()
            r.weekly_hours.all().delete()
            WeeklyHours.objects.bulk_create(WeeklyHours(resource=r, weekday=w['weekday'], start_minute=w['start'],
                                                        end_minute=w['end']) for w in s.validated_data['weekly'])
            r.overrides.all().delete()
            DateOverride.objects.bulk_create(
                DateOverride(resource=r, date=o['date'], reason=o['reason'],
                             hours=[[h['start'], h['end']] for h in o['hours']])
                for o in s.validated_data['overrides'])
        return Response(schedule_out(self._resource(request, ref)))


class OverrideView(APIView):
    @extend_schema(request=OverrideSerializer, responses=ScheduleSerializer,
                   description='Set one date\'s hours (empty `hours`: closed that day).')
    def put(self, request, ref, day):
        r = get_object_or_404(Resource, platform=platform_of(request), external_ref=ref)
        s = OverrideSerializer(data={**request.data, 'date': day})
        s.is_valid(raise_exception=True)
        DateOverride.objects.update_or_create(
            resource=r, date=s.validated_data['date'],
            defaults={'hours': [[h['start'], h['end']] for h in s.validated_data['hours']],
                      'reason': s.validated_data['reason']})
        return Response(schedule_out(Resource.objects.prefetch_related('weekly_hours', 'overrides').get(pk=r.pk)))

    @extend_schema(responses={204: None}, description='Back to the weekly hours for that date.')
    def delete(self, request, ref, day):
        r = get_object_or_404(Resource, platform=platform_of(request), external_ref=ref)
        DateOverride.objects.filter(resource=r, date=day).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

class AvailabilityView(APIView):
    @extend_schema(request=AvailabilitySerializer, responses=inline_serializer('Availability', {
        'timezone': serializers.CharField(), 'slots': serializers.ListField(child=serializers.DictField())}))
    def post(self, request):
        platform = platform_of(request)
        s = AvailabilitySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        reqs = resolve_requirements(platform, d['requirements'], active_only=True)
        tzname = d.get('timezone') or reqs[0].resources[0].timezone
        tz = ZoneInfo(tzname)
        start, end = window(d['start'], d['end'], tz)
        slots = availability.search(platform, reqs, start, end, duration_minutes=d['duration_minutes'],
                                    step_minutes=d['step_minutes'], tz=tz, now=timezone.now(),
                                    include_unavailable=d['include_unavailable'])
        return Response({'timezone': tzname, 'groups': [[r.external_ref for r in q.resources] for q in reqs],
                         'slots': slots})


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------

class BookingList(APIView):
    @extend_schema(parameters=[
        OpenApiParameter('resource', str, description='Your resource ref'),
        OpenApiParameter('status', str, enum=[s.value for s in BookingStatus]),
        OpenApiParameter('from', str, description='Bookings ending after this instant'),
        OpenApiParameter('to', str, description='Bookings starting before this instant'),
        OpenApiParameter('external_ref', str), OpenApiParameter('subject_ref', str),
    ], responses=BookingSerializer(many=True))
    def get(self, request):
        platform = platform_of(request)
        qs = bookings_qs(platform)
        q = request.query_params
        if q.get('resource'):
            qs = qs.filter(allocations__resource__external_ref=q['resource']).distinct()
        if q.get('status'):
            qs = qs.filter(status__in=q['status'].split(','))
        for name in ('external_ref', 'subject_ref'):
            if q.get(name):
                qs = qs.filter(**{name: q[name]})
        if t := parse_instant(request, 'from'):
            qs = qs.filter(end_at__gt=t)
        if t := parse_instant(request, 'to'):
            qs = qs.filter(start_at__lt=t)
        page = PageNumberPagination()
        return page.get_paginated_response(BookingSerializer(page.paginate_queryset(qs, request, self), many=True).data)

    @extend_schema(request=BookingCreateSerializer, responses={201: BookingSerializer}, parameters=[IDEMPOTENCY],
                   description='Hold time (or book it outright with `confirm: true`). 409 `slot_unavailable` '
                               'says which group could not be filled and why.')
    def post(self, request):
        platform = platform_of(request)

        def work():
            s = BookingCreateSerializer(data=request.data)
            s.is_valid(raise_exception=True)
            d = s.validated_data
            b = holds.create(
                platform, requirements=resolve_requirements(platform, d['requirements'], active_only=False),
                start=d['start'], end=d['end'], hold_minutes=d.get('hold_minutes', settings.HOLD_DEFAULT_MINUTES),
                confirm=d['confirm'], subject_ref=d['subject_ref'], external_ref=d['external_ref'],
                metadata=d['metadata'], override_rules=d['override_rules'])
            return booking_response(platform, b, status.HTTP_201_CREATED)
        return idempotency.run(request, work)


class BookingDetail(APIView):
    @extend_schema(responses=BookingSerializer)
    def get(self, request, pk):
        return Response(BookingSerializer(get_object_or_404(bookings_qs(platform_of(request)), pk=pk)).data)


class _Action(APIView):
    serializer = None

    def act(self, platform, pk, data):
        raise NotImplementedError

    def post(self, request, pk):
        platform = platform_of(request)

        def work():
            data = {}
            if self.serializer:
                s = self.serializer(data=request.data)
                s.is_valid(raise_exception=True)
                data = s.validated_data
            return booking_response(platform, self.act(platform, pk, data))
        return idempotency.run(request, work)


@extend_schema(request=None, responses=BookingSerializer, parameters=[IDEMPOTENCY],
               description='Confirm a hold. An expired hold is taken back if its time is still free; '
                           'otherwise 409 `slot_taken`.')
class ConfirmView(_Action):
    def act(self, platform, pk, data):
        return holds.confirm(platform, pk)


@extend_schema(request=ExtendSerializer, responses=BookingSerializer, parameters=[IDEMPOTENCY],
               description='Hold for `minutes` more from now (e.g. payment still in progress).')
class ExtendView(_Action):
    serializer = ExtendSerializer

    def act(self, platform, pk, data):
        return holds.extend(platform, pk, data['minutes'])


@extend_schema(request=CancelSerializer, responses=BookingSerializer, parameters=[IDEMPOTENCY])
class CancelView(_Action):
    serializer = CancelSerializer

    def act(self, platform, pk, data):
        return holds.cancel(platform, pk, data['reason'])


@extend_schema(request=RescheduleSerializer, responses=BookingSerializer, parameters=[IDEMPOTENCY],
               description='Move to a new time (and optionally new resources). If it is taken, nothing changes.')
class RescheduleView(_Action):
    serializer = RescheduleSerializer

    def act(self, platform, pk, data):
        reqs = resolve_requirements(platform, data['requirements'], active_only=False) \
            if data.get('requirements') else None
        return holds.reschedule(platform, pk, start=data['start'], end=data['end'], requirements=reqs,
                                override_rules=data['override_rules'])


# ---------------------------------------------------------------------------
# Blocks and closures
# ---------------------------------------------------------------------------

class BlockList(APIView):
    @extend_schema(parameters=[OpenApiParameter('resource', str), OpenApiParameter('from', str),
                               OpenApiParameter('to', str)], responses=BlockSerializer(many=True))
    def get(self, request):
        qs = Block.objects.filter(platform=platform_of(request)).select_related('resource')
        if ref := request.query_params.get('resource'):
            qs = qs.filter(resource__external_ref=ref)
        if t := parse_instant(request, 'from'):
            qs = qs.filter(period__endswith__gt=t)
        if t := parse_instant(request, 'to'):
            qs = qs.filter(period__startswith__lt=t)
        page = PageNumberPagination()
        return page.get_paginated_response(BlockSerializer(page.paginate_queryset(qs, request, self), many=True).data)

    @extend_schema(request=BlockCreateSerializer, responses={201: BlockSerializer}, parameters=[IDEMPOTENCY],
                   description='409 `slot_unavailable` lists the bookings already in that time.')
    def post(self, request):
        platform = platform_of(request)

        def work():
            s = BlockCreateSerializer(data=request.data)
            s.is_valid(raise_exception=True)
            d = s.validated_data
            r = get_object_or_404(Resource, platform=platform, external_ref=d['resource'])
            blk = holds.create_block(platform, r, start=d['start'], end=d['end'], kind=d['kind'],
                                     reason=d['reason'], external_ref=d['external_ref'])
            return Response(BlockSerializer(blk).data, status=status.HTTP_201_CREATED)
        return idempotency.run(request, work)


class BlockDetail(APIView):
    @extend_schema(responses={204: None})
    def delete(self, request, pk):
        get_object_or_404(Block, platform=platform_of(request), pk=pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ClosureList(APIView):
    @extend_schema(responses=ClosureSerializer(many=True))
    def get(self, request):
        qs = Closure.objects.filter(platform=platform_of(request))
        page = PageNumberPagination()
        return page.get_paginated_response(ClosureSerializer(page.paginate_queryset(qs, request, self),
                                                             many=True).data)

    @extend_schema(request=ClosureCreateSerializer, responses={201: ClosureSerializer},
                   description='Shut the platform (or some kinds of resource) for a period. Existing bookings '
                               'are not cancelled; the ones inside it are listed as `affected_bookings`.')
    def post(self, request):
        platform = platform_of(request)
        s = ClosureCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        c = Closure.objects.create(platform=platform, period=holds.period(d['start'], d['end']), kinds=d['kinds'],
                                   reason=d['reason'])
        affected = Booking.objects.filter(
            platform=platform, status__in=[BookingStatus.HELD, BookingStatus.CONFIRMED],
            allocations__active=True, start_at__lt=d['end'], end_at__gt=d['start'])
        if d['kinds']:
            affected = affected.filter(allocations__resource__kind__in=d['kinds'])
        data = ClosureSerializer(c).data
        data['affected_bookings'] = [{'id': str(b.id), 'external_ref': b.external_ref}
                                     for b in affected.distinct()]
        return Response(data, status=status.HTTP_201_CREATED)


class ClosureDetail(APIView):
    @extend_schema(responses={204: None})
    def delete(self, request, pk):
        get_object_or_404(Closure, platform=platform_of(request), pk=pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
