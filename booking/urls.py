from django.urls import path

from . import views

urlpatterns = [
    path('resources/', views.ResourceList.as_view()),
    path('resources/<str:ref>/', views.ResourceDetail.as_view()),
    path('resources/<str:ref>/schedule/', views.ScheduleView.as_view()),
    path('resources/<str:ref>/overrides/<str:day>/', views.OverrideView.as_view()),
    path('availability/', views.AvailabilityView.as_view()),
    path('bookings/', views.BookingList.as_view()),
    path('bookings/<uuid:pk>/', views.BookingDetail.as_view()),
    path('bookings/<uuid:pk>/confirm/', views.ConfirmView.as_view()),
    path('bookings/<uuid:pk>/extend/', views.ExtendView.as_view()),
    path('bookings/<uuid:pk>/cancel/', views.CancelView.as_view()),
    path('bookings/<uuid:pk>/reschedule/', views.RescheduleView.as_view()),
    path('blocks/', views.BlockList.as_view()),
    path('blocks/<uuid:pk>/', views.BlockDetail.as_view()),
    path('closures/', views.ClosureList.as_view()),
    path('closures/<uuid:pk>/', views.ClosureDetail.as_view()),
]
