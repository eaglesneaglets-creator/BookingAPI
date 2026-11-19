"""
Back office: platforms and keys are managed here; bookings are read-only
(they change through the API, so every change keeps its guarantees and webhooks).
"""

from django.contrib import admin

from .models import ApiKey, Block, Booking, Closure, Platform, Resource, WebhookDelivery, WeeklyHours


class ApiKeyInline(admin.TabularInline):
    model = ApiKey
    extra = 0
    fields = ['name', 'prefix', 'created_at', 'last_used_at', 'revoked_at']
    readonly_fields = ['name', 'prefix', 'created_at', 'last_used_at']
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False  # keys are issued with `manage.py create_platform`, which shows the secret once


@admin.register(Platform)
class PlatformAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'webhook_url', 'is_active', 'created_at']
    readonly_fields = ['created_at']
    exclude = ['webhook_secret_encrypted']
    inlines = [ApiKeyInline]


class WeeklyHoursInline(admin.TabularInline):
    model = WeeklyHours
    extra = 0


@admin.register(Resource)
class ResourceAdmin(admin.ModelAdmin):
    list_display = ['name', 'kind', 'platform', 'external_ref', 'timezone', 'is_active']
    list_filter = ['platform', 'kind', 'is_active']
    search_fields = ['name', 'external_ref']
    inlines = [WeeklyHoursInline]


class ReadOnly(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Booking)
class BookingAdmin(ReadOnly):
    list_display = ['start_at', 'end_at', 'platform', 'status', 'external_ref', 'subject_ref', 'hold_expires_at']
    list_filter = ['platform', 'status']
    search_fields = ['external_ref', 'subject_ref', 'id']
    date_hierarchy = 'start_at'


@admin.register(Block)
class BlockAdmin(ReadOnly):
    list_display = ['resource', 'period', 'kind', 'reason', 'platform']
    list_filter = ['platform', 'kind']


@admin.register(Closure)
class ClosureAdmin(ReadOnly):
    list_display = ['period', 'kinds', 'reason', 'platform']
    list_filter = ['platform']


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(ReadOnly):
    list_display = ['created_at', 'platform', 'event_type', 'attempts', 'delivered_at', 'last_error']
    list_filter = ['platform', 'event_type']
