from pathlib import Path

from django.contrib.auth.views import LogoutView
from django.http import FileResponse, JsonResponse
from django.urls import path
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_safe

from accounts import views as account_views
from bookings import views
from bookings.booking_detail import booking_detail
from bookings.brigades import brigade_edit, brigades
from bookings.calendar import calendar, calendar_detail
from bookings.participant_views import booking_participant
from bookings.people import people
from bookings.review_actions import review_detail
from bookings.ride_settings import rides_settings
from bookings.ride_simulation import (
    ride_simulation,
    ride_simulation_open,
    ride_simulation_response,
    ride_simulation_script,
)
from bookings.rides import ride_detail, rides, rides_sync
from mailimport.mailbox_views import mailbox_view
from mailimport.receipt_views import mail_receipt, mail_receipt_recheck, mail_receipts
from mailimport.views import manual_import, preview, sample_eml


@require_safe
def health(request):
    return JsonResponse({"status": "ok"})


@require_safe
@cache_control(public=True, max_age=3600)
def live_script(request):
    return FileResponse(Path(__file__).with_name("live.js").open("rb"), content_type="text/javascript; charset=utf-8")


@require_safe
@cache_control(public=True, max_age=3600)
def workspace_chrome_script(request):
    return FileResponse(
        Path(__file__).with_name("workspace_chrome.js").open("rb"), content_type="text/javascript; charset=utf-8",
    )


@require_safe
@cache_control(public=True, max_age=3600)
def calendar_timeline_script(request):
    return FileResponse(
        Path(__file__).with_name("calendar_timeline.js").open("rb"), content_type="text/javascript; charset=utf-8",
    )


@require_safe
@cache_control(public=True, max_age=3600)
def favicon(request):
    return FileResponse(Path(__file__).with_name("favicon.png").open("rb"), content_type="image/png")


urlpatterns = [
    path("assets/live.js", live_script, name="live_script"),
    path("assets/workspace-chrome.js", workspace_chrome_script, name="workspace_chrome_script"),
    path("assets/calendar-timeline.js", calendar_timeline_script, name="calendar_timeline_script"),
    path("assets/ride-simulation.js", ride_simulation_script, name="ride_simulation_script"),
    path("favicon.png", favicon, name="favicon"),
    path("anmelden/", account_views.WorkspaceLoginView.as_view(), name="login"),
    path("abmelden/", LogoutView.as_view(), name="logout"),
    path("konto/", account_views.account_home, name="account"),
    path("konto/passwort/", account_views.AccountPasswordChangeView.as_view(), name="account_password_change"),
    path("benutzer/", account_views.account_list, name="account_list"),
    path("benutzer/neu/", account_views.account_create, name="account_create"),
    path("benutzer/<int:user_id>/", account_views.account_edit, name="account_edit"),
    path("benutzer/<int:user_id>/passwort/", account_views.account_password_reset, name="account_password_reset"),
    path("", views.overview, name="overview"),
    path("buchungen/", views.bookings, name="bookings"),
    path("buchungen/<int:seat_id>/", booking_detail, name="booking_detail"),
    path("buchungen/<int:seat_id>/teilnehmer/", booking_participant, name="booking_participant"),
    path("personen/", people, name="people"),
    path("kalender/", calendar, name="calendar"),
    path("kalender/<int:session_id>/", calendar_detail, name="calendar_detail"),
    path("fahrgemeinschaften/", rides, name="rides"),
    path("fahrgemeinschaften/abgleichen/", rides_sync, name="rides_sync"),
    path("fahrgemeinschaften/einstellungen/", rides_settings, name="rides_settings"),
    path("fahrgemeinschaften/simulation/", ride_simulation, name="ride_simulation"),
    path("fahrgemeinschaften/simulation/antwort/", ride_simulation_open, name="ride_simulation_open"),
    path(
        "fahrgemeinschaften/simulation/antwort/<int:invitation_id>/",
        ride_simulation_response,
        name="ride_simulation_response",
    ),
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
