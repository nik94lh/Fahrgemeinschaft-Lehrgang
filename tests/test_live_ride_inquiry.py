from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from bookings.models import BookingStatus, Course, CourseSeat, CourseSession, Participant, RideCase, RideMailDelivery, RideNotice, School
from bookings.ride_mail import deliver_ride_mail
from bookings.ride_participation import invite_code, participation_for_code, portal_overview, record_ride_reply, run_automatic_ride_inquiries
from bookings.rides_matching import sync_ride_cases
from mailimport.models import ParticipantRecognitionConfiguration
from tests.workspace_client import WorkspaceTestCase


@override_settings(HTTPS=True, RIDE_PORTAL_HOST="mitfahren.example.com")
class LiveRideInquiryTests(WorkspaceTestCase):
    def make_case(self, participant_count=2):
        starts = timezone.now() + timedelta(days=3)
        session = CourseSession.objects.create(
            course=Course.objects.create(name="Fiktiver Lehrgang"), school=School.WUERZBURG,
            starts_at=starts, ends_at=starts + timedelta(days=1),
        )
        for number in range(participant_count):
            address = f"person-{number}@example.com"
            person = Participant.objects.create(name=f"Fiktive Person {number}", email=address)
            CourseSeat.objects.create(session=session, participant=person, status=BookingStatus.CONFIRMED)
        sync_ride_cases()
        return RideCase.objects.get(is_test=False)

    @patch("mailimport.outbound.outbound_enabled", return_value=True)
    def test_manual_button_starts_live_inquiries_and_portal_keeps_valid_codes(self, smtp):
        case = self.make_case()
        listing = self.client.get(reverse("rides"), secure=True)
        self.assertContains(listing, "Abfrage starten")
        start = reverse("ride_live_inquiry_start", args=[case.pk])
        self.assertEqual(self.client.get(start, secure=True).status_code, 405)
        self.assertRedirects(
            self.client.post(start, secure=True),
            reverse("ride_detail", args=[case.pk], query={"abfrage": "vorbereitet"}), fetch_redirect_response=False,
        )
        first, second = case.participation_requests.order_by("pk")
        self.assertNotContains(self.client.get(reverse("ride_detail", args=[case.pk]), secure=True), invite_code(first))
        self.assertNotContains(self.client.get(reverse("rides"), secure=True), "Abfrage starten")

        smtp.return_value = False
        self.assertEqual(participation_for_code(invite_code(first)), first)
        record_ride_reply(first.pk, first.token_digest, answer="yes", phone="+49 123 456789", channels=["call"], consent=True)
        self.assertEqual(participation_for_code(invite_code(second)), second)
        record_ride_reply(second.pk, second.token_digest, answer="yes", channels=["email"], consent=True)
        first.refresh_from_db()
        self.assertEqual(first.phone, "+49 123 456789")
        self.assertEqual(len(portal_overview(first)[1]), 1)
        ParticipantRecognitionConfiguration.objects.create(pk=1, functional_addresses=[second.email])
        self.assertIsNone(participation_for_code(invite_code(second)))
        self.assertEqual(portal_overview(first)[1], [])

    @patch("mailimport.outbound.outbound_enabled", return_value=True)
    @patch("mailimport.outbound.outbound_automation_enabled", return_value=False)
    def test_automatic_mode_cannot_be_bypassed_by_direct_manual_post(self, automatic, smtp):
        case = self.make_case()
        self.client.post(reverse("ride_automation"), {"enabled": "1"}, secure=True)
        listing = self.client.get(reverse("rides"), secure=True)
        self.assertNotContains(listing, "Abfrage starten")
        self.client.post(reverse("ride_live_inquiry_start", args=[case.pk]), secure=True)
        self.assertFalse(case.participation_requests.exists())
        self.assertEqual(run_automatic_ride_inquiries(), 0)
        automatic.return_value = True
        self.assertEqual(run_automatic_ride_inquiries(), 2)
        self.assertEqual(run_automatic_ride_inquiries(), 0)

    @patch("mailimport.outbound.outbound_enabled", return_value=True)
    def test_reclassified_functional_address_stops_invitation_even_before_match_refresh(self, smtp):
        case = self.make_case()
        ParticipantRecognitionConfiguration.objects.create(pk=1, functional_addresses=["person-0@example.com"])
        self.client.post(reverse("ride_live_inquiry_start", args=[case.pk]), secure=True)
        self.assertFalse(case.participation_requests.exists())

    @patch("mailimport.outbound.outbound_enabled", return_value=True)
    @patch("mailimport.outbound.send_ride_mail")
    def test_resend_is_scoped_to_one_notice_and_reuses_access_code(self, send, smtp):
        case = self.make_case()
        self.client.post(reverse("ride_live_inquiry_start", args=[case.pk]), secure=True)
        detail = reverse("ride_detail", args=[case.pk])
        notice = case.participation_requests.order_by("pk").first().notices.get()
        resend = reverse("ride_notice_resend", args=[case.pk, notice.pk])
        self.assertNotContains(self.client.get(detail, secure=True), resend)
        self.assertEqual(deliver_ride_mail(), 2)
        first_mail = send.call_args_list[0].kwargs
        self.assertContains(self.client.get(detail, secure=True), resend)
        self.assertEqual(self.client.get(resend, secure=True).status_code, 405)
        wrong_case = reverse("ride_notice_resend", args=[case.pk + 1, notice.pk])
        self.assertEqual(self.client.post(wrong_case, secure=True).status_code, 404)
        self.assertRedirects(
            self.client.post(resend, secure=True), f"{detail}?abfrage=erneut-vorgemerkt", fetch_redirect_response=False,
        )
        self.assertEqual(deliver_ride_mail(), 1)
        retry = send.call_args.kwargs
        self.assertEqual(retry["recipient"], first_mail["recipient"])
        self.assertEqual(retry["body"], first_mail["body"])
        self.assertNotEqual(retry["message_id"], first_mail["message_id"])
        self.assertEqual(case.participation_requests.count(), 2)

    @patch("mailimport.outbound.outbound_enabled", return_value=True)
    @patch("mailimport.outbound.send_ride_mail")
    def test_pending_provider_email_for_new_peer_is_dropped_after_withdrawal(self, send, smtp):
        case = self.make_case(participant_count=3)
        self.client.post(reverse("ride_live_inquiry_start", args=[case.pk]), secure=True)
        entries = list(case.participation_requests.order_by("pk"))
        for entry in entries:
            record_ride_reply(entry.pk, entry.token_digest, answer="yes", channels=["email"], consent=True)
        third = entries[-1]
        record_ride_reply(third.pk, third.token_digest, answer="no")
        self.assertEqual(deliver_ride_mail(), 7)
        self.assertFalse(any("Weitere Zusage" in call.kwargs["subject"] for call in send.call_args_list))
        self.assertEqual(RideMailDelivery.objects.filter(
            notice__kind=RideNotice.Kind.NEW_PEER, status=RideMailDelivery.Status.CANCELLED,
        ).count(), 2)
