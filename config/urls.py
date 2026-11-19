from django.conf import settings
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAdminUser

# The API docs are for people: shown to staff signed in to this service's admin.
DOCS = {'authentication_classes': [SessionAuthentication], 'permission_classes': [IsAdminUser]}


def health(_request):
    return JsonResponse({'status': 'ok', 'environment': settings.ENVIRONMENT_NAME})


admin.site.site_header = 'Booking API'
admin.site.site_title = 'Booking API'

urlpatterns = [
    path('health/', health),
    path('v1/', include('booking.urls')),
    path('schema/', SpectacularAPIView.as_view(**DOCS), name='schema'),
    path('docs/', SpectacularSwaggerView.as_view(url_name='schema', **DOCS), name='docs'),
    path(settings.ADMIN_URL, admin.site.urls),
]
