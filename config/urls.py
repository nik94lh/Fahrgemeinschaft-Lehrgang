from pathlib import Path

from django.http import FileResponse, JsonResponse
from django.urls import path
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_safe

from bookings import views
from bookings.brigades import brigade_edit, brigades
from bookings.calendar import calendar, calendar_detail
from bookings.participant_views import booking_participant
from bookings.review_actions import review_detail
from bookings.rides import ride_detail, rides, rides_sync
from mailimport.mailbox_views import mailbox_view
from mailimport.receipt_views import mail_receipt, mail_receipt_recheck, mail_receipts
from mailimport.views import manual_import, preview, sample_eml


def health(request):
    return JsonResponse({"status": "ok"})


@require_safe
@cache_control(public=True, max_age=3600)
def live_script(request):
    return FileResponse(Path(__file__).with_name("live.js").open("rb"), content_type="text/javascript; charset=utf-8")


urlpatterns = [
    path("assets/live.js", live_script, name="live_script"),
    path("", views.overview, name="overview"),
    path("buchungen/", views.bookings, name="bookings"),
    path("buchungen/<int:seat_id>/teilnehmer/", booking_participant, name="booking_participant"),
    path("kalender/", calendar, name="calendar"),
    path("kalender/<int:session_id>/", calendar_detail, name="calendar_detail"),
    path("fahrgemeinschaften/", rides, name="rides"),
    path("fahrgemeinschaften/abgleichen/", rides_sync, name="rides_sync"),
    path("fahrgemeinschaften/<int:case_id>/", ride_detail, name="ride_detail"),
    path("feuerwehren/", brigades, name="brigades"),
    path("feuerwehren/neu/", brigade_edit, name="brigade_create"),
    path("feuerwehren/<int:brigade_id>/", brigade_edit, name="brigade_edit"),
    path("klaerung/", views.review, name="review"),
    path("klaerung/<int:event_id>/", review_detail, name="review_detail"),
    path("vorschau/", preview, name="preview"),
    path("importieren/", manual_import, name="manual_import"),
    path("testmail.eml", sample_eml, name="sample_eml"),
    path("postfach/", mailbox_view, name="mailbox"),
    path("mail-eingang/", mail_receipts, name="mail_receipts"),
    path("mail-eingang/<int:receipt_id>/", mail_receipt, name="mail_receipt"),
    path("mail-eingang/<int:receipt_id>/adresse-pruefen/", mail_receipt_recheck, name="mail_receipt_recheck"),
    path("health/", health, name="health"),
]
