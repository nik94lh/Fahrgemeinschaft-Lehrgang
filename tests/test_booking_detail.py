from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from bookings.models import BookingHistory, BookingStatus, Course, CourseSeat, CourseSession, EmailEvent, Participant, School
from mailimport.models import MailReceipt
from tests.workspace_client import WorkspaceTestCase


class BookingDetailTests(WorkspaceTestCase):
    @classmethod
    def setUpTestData(cls):
        start = timezone.now() + timedelta(days=6)
        current = Participant.objects.create(name="Fiktive Testperson", email="fiktive.testperson@example.invalid")
        course = Course.objects.create(name="Übungslehrgang Brandwache")
        session = CourseSession.objects.create(
            course=course, school=School.GERETSRIED, starts_at=start, ends_at=start + timedelta(hours=3),
        )
        cls.seat = CourseSeat.objects.create(session=session, participant=current, status=BookingStatus.CONFIRMED)
        different = Course.objects.create(name="Unbeteiligter Musterlehrgang")
        other_session = CourseSession.objects.create(
            course=different, school=School.WUERZBURG, starts_at=start, ends_at=start + timedelta(hours=4),
        )
        cls.other_seat = CourseSeat.objects.create(session=other_session, participant=current)
        cls.confirmation = EmailEvent.objects.create(
            seat=cls.seat, sha256="1" * 64, kind=EmailEvent.Kind.CONFIRMATION, state=EmailEvent.State.APPLIED,
        )
        BookingHistory.objects.create(
            seat=cls.seat, source_event=cls.confirmation, status=BookingStatus.CONFIRMED,
            previous_status=BookingStatus.APPROVED, participant=current,
        )
        cls.receipt = MailReceipt.objects.create(
            source_id="synthetic", uid_validity=1, uid=1, event=cls.confirmation,
            fields={"participant_email": current.email}, outcome=MailReceipt.Outcome.APPLIED,
        )

    def detail_url(self, seat=None):
        return f"/buchungen/{(seat or self.seat).pk}/"

    def test_shows_only_this_booking_and_offers_explicit_profile_button(self):
        response = self.client.get(self.detail_url())

        self.assertContains(response, f"Buchungs-ID {self.seat.pk}")
        self.assertContains(response, self.seat.session.course.name)
        self.assertContains(response, "SFS Geretsried")
        self.assertContains(response, "Teilnehmerprofil ansehen")
        self.assertContains(response, reverse("booking_participant", args=[self.seat.pk]))
        self.assertNotContains(response, self.other_seat.session.course.name)
        self.assertNotContains(response, self.seat.participant.email)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(self.client.post(self.detail_url()).status_code, 405)

    def test_same_participant_or_name_on_other_bookings_does_not_merge_status_or_events(self):
        homonym = Participant.objects.create(name=self.seat.participant.name, email="namensgleich@example.invalid")
        confusable_seats = (
            self.other_seat,
            CourseSeat.objects.create(session=self.seat.session, participant=homonym),
        )
        for seat, suffix in zip(confusable_seats, ("6", "7")):
            event = EmailEvent.objects.create(
                seat=seat, sha256=suffix * 64, kind=EmailEvent.Kind.CANCELLATION, state=EmailEvent.State.APPLIED,
            )
            BookingHistory.objects.create(
                seat=seat, source_event=event, status=BookingStatus.CANCELLED, participant=seat.participant,
            )

        response = self.client.get(self.detail_url())

        self.assertContains(response, "Teilnahmebestätigung")
        self.assertNotContains(response, "Stornierung")
        self.assertNotContains(response, "Storniert")
        self.assertNotContains(response, homonym.email)

    def test_mail_history_is_chronological_without_duplicate_status_changes(self):
        EmailEvent.objects.create(
            seat=self.seat, sha256="2" * 64, kind=EmailEvent.Kind.CANCELLATION, state=EmailEvent.State.NEEDS_REVIEW,
            review_context={"participant_name": "Privater Ausschnitt aus einem Klärungsfall"},
        )
        EmailEvent.objects.create(
            seat=self.seat, sha256="3" * 64, kind=EmailEvent.Kind.SCHEDULE_CHANGE,
            state=EmailEvent.State.DISMISSED, review_resolution=EmailEvent.ReviewResolution.NOT_RELEVANT,
        )
        EmailEvent.objects.create(
            seat=self.seat, sha256="4" * 64, kind=EmailEvent.Kind.APPROVAL, state=EmailEvent.State.APPLIED,
        )

        response = self.client.get(self.detail_url())

        self.assertContains(response, "Teilnahmebestätigung", count=1)
        self.assertContains(response, "Genehmigt → Bestätigt", count=1)
        self.assertContains(response, "Stornierung")
        self.assertContains(response, "Klärungsbedarf")
        self.assertContains(response, "Verworfen")
        self.assertContains(response, "Kein Statuswechsel protokolliert", count=1)
        self.assertNotContains(response, "Privater Ausschnitt aus einem Klärungsfall")
        content = response.content.decode()
        self.assertLess(content.index("Teilnahmebestätigung"), content.index("Stornierung"))
        self.assertLess(content.index("Stornierung"), content.index("Terminänderung"))

    def test_unrelated_reassignment_source_only_shows_local_history_change(self):
        event = EmailEvent.objects.create(
            seat=self.other_seat, sha256="5" * 64, kind=EmailEvent.Kind.REASSIGNMENT,
            state=EmailEvent.State.REVIEWED, review_context={"participant_name": "Andere geheime Person"},
        )
        BookingHistory.objects.create(
            seat=self.seat, source_event=event, status=BookingStatus.REASSIGNED,
            previous_status=BookingStatus.CONFIRMED, previous_participant=self.seat.participant,
        )
        other_receipt = MailReceipt.objects.create(source_id="synthetic", uid_validity=1, uid=2, event=event)

        with override_settings(HTTPS=True):
            response = self.client.get(self.detail_url(), secure=True)

        self.assertContains(response, "Bestätigt → Umgemeldet")
        self.assertNotContains(response, "Andere geheime Person")
        self.assertNotContains(response, self.other_seat.session.course.name)
        self.assertNotContains(response, reverse("mail_receipt", args=[other_receipt.pk]))

    def test_receipt_links_require_secure_connection_and_mail_permission(self):
        receipt_url = reverse("mail_receipt", args=[self.receipt.pk])
        self.assertNotContains(self.client.get(self.detail_url(), secure=True), receipt_url)
        with override_settings(HTTPS=True):
            self.assertNotContains(self.client.get(self.detail_url()), receipt_url)
            self.assertContains(self.client.get(self.detail_url(), secure=True), receipt_url)

            user = get_user_model().objects.create_user("booking-details-only")
            user.user_permissions.add(Permission.objects.get(content_type__app_label="accounts", codename="view_bookings"))
            self.client.force_login(user)
            self.assertNotContains(self.client.get(self.detail_url(), secure=True), receipt_url)

    def test_missing_participant_or_history_is_supported_and_requires_booking_right(self):
        empty = CourseSeat.objects.create(session=self.seat.session)
        response = self.client.get(self.detail_url(empty))
        self.assertContains(response, "Keine Person zugeordnet")
        self.assertContains(response, "Noch keine Ereignisse protokolliert.")
        self.assertNotContains(response, reverse("booking_participant", args=[empty.pk]))

        without_permission = get_user_model().objects.create_user("no-booking-details")
        self.client.force_login(without_permission)
        self.assertEqual(self.client.get(self.detail_url()).status_code, 403)
