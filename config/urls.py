from django.http import JsonResponse
from django.urls import path

from bookings import views
from bookings.brigades import brigade_edit, brigades
from bookings.calendar import calendar
from bookings.review_actions import review_detail
from bookings.rides import rides
from mailimport.mailbox_views import mailbox_view
from mailimport.views import manual_import, preview, sample_eml


def health(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("", views.overview, name="overview"),
    path("buchungen/", views.bookings, name="bookings"),
    path("kalender/", calendar, name="calendar"),
    path("fahrgemeinschaften/", rides, name="rides"),
    path("feuerwehren/", brigades, name="brigades"),
    path("feuerwehren/neu/", brigade_edit, name="brigade_create"),
    path("feuerwehren/<int:brigade_id>/", brigade_edit, name="brigade_edit"),
    path("klaerung/", views.review, name="review"),
    path("klaerung/<int:event_id>/", review_detail, name="review_detail"),
    path("vorschau/", preview, name="preview"),
    path("importieren/", manual_import, name="manual_import"),
    path("testmail.eml", sample_eml, name="sample_eml"),
    path("postfach/", mailbox_view, name="mailbox"),
    path("health/", health, name="health"),
]
