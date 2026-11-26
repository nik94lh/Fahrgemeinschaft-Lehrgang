from datetime import timedelta

from django.db import connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from bookings.models import BookingStatus, Course, CourseSeat, CourseSession, Participant, RideCase, School
from bookings.ride_participation import invite_code, record_ride_reply, start_ride_inquiry
from bookings.rides_matching import sync_ride_cases
from tests.workspace_client import WorkspaceTestCase


@override_settings(HTTPS=True, RIDE_PORTAL_HOST="mitfahren.bms-ab.de")
class RideInquiryCardTests(WorkspaceTestCase):
    def make_case(self, *, days=3, is_test=True):
        starts = timezone.now() + timedelta(days=days)
        session = CourseSession.objects.create(
            course=Course.objects.create(name="Synthetischer Lehrgang"),
            school=School.WUERZBURG, starts_at=starts, ends_at=starts + timedelta(days=1),
        )
        for index in range(3):
            participant = Participant.objects.create(
                name=f"Fiktive Person {days}-{index}", email=f"person-{days}-{index}@example.invalid",
            )
            CourseSeat.objects.create(
                session=session, participant=participant, status=BookingStatus.CONFIRMED, is_test=is_test,
            )
        sync_ride_cases()
        return RideCase.objects.get(travel_date=timezone.localdate(starts), is_test=is_test)

    def listing(self):
        return self.client.get(reverse("rides"), secure=True)

    def start(self, case):
        start_ride_inquiry(case.pk)
        return list(case.participation_requests.order_by("pk"))

    def reply(self, entry, answer):
        record_ride_reply(entry.pk, entry.token_digest, answer=answer, channels=["email"], consent=True)

    def assert_card_state(self, response, state):
        self.assertContains(response, f'<article class="ride-card ride-card--{state}"')

    def test_card_tracks_open_answers_and_current_contact_sharing(self):
        case = self.make_case()
        listing = self.listing()
        self.assertContains(listing, "Noch nicht gestartet")
        self.assert_card_state(listing, "not-started")
        first, second, third = self.start(case)
        listing = self.listing()
        self.assertContains(listing, "Antworten ausstehend")
        self.assert_card_state(listing, "awaiting-replies")
        self.assertContains(listing, "3 Einladungen vorbereitet")
        self.assertContains(listing, "3 offen")
        self.assertContains(listing, "0 zugesagt")
        self.assertContains(listing, "0 abgesagt")

        self.reply(first, "yes")
        self.reply(third, "no")
        listing = self.listing()
        self.assertContains(listing, "1 offen")
        self.assertContains(listing, "1 zugesagt")
        self.assertContains(listing, "1 abgesagt")
        self.assertNotContains(listing, "Kontakte freigegeben")

        self.reply(second, "yes")
        listing = self.listing()
        self.assertContains(listing, "Kontakte freigegeben")
        self.assert_card_state(listing, "contacts-released")
        self.assertContains(listing, "0 offen")
        self.assertContains(listing, "2 zugesagt")
        self.assertContains(listing, "1 abgesagt")
        self.assertNotContains(listing, invite_code(first))
        self.assertNotContains(listing, first.email)
        detail = self.client.get(reverse("ride_detail", args=[case.pk]), secure=True)
        self.assertContains(detail, "Kontakte freigegeben")

        self.reply(second, "no")
        listing = self.listing()
        self.assertContains(listing, "Kontaktfreigabe beendet")
        self.assert_card_state(listing, "no-match")
        self.assertContains(listing, "1 zugesagt")
        self.assertContains(listing, "2 abgesagt")
        self.assertNotContains(listing, "Kontakte freigegeben")

    def test_coloring_prioritizes_current_contacts_then_unanswered_invites(self):
        first, second, third = self.start(self.make_case())
        self.reply(first, "yes")
        self.reply(second, "yes")
        listing = self.listing()
        self.assert_card_state(listing, "contacts-released")
        self.assertContains(listing, "1 offen")

        self.reply(second, "no")
        listing = self.listing()
        self.assert_card_state(listing, "awaiting-replies")
        self.assertContains(listing, "Kontaktfreigabe beendet, weitere Antworten offen")

        self.reply(first, "no")
        listing = self.listing()
        self.assert_card_state(listing, "awaiting-replies")
        self.assertContains(listing, "1 offen")
        self.assertContains(listing, "2 abgesagt")

        self.reply(third, "yes")
        self.assert_card_state(self.listing(), "no-match")

    def test_two_old_yes_answers_do_not_show_contacts_as_released(self):
        case = self.make_case()
        first, second, _ = self.start(case)
        self.reply(first, "yes")
        self.reply(second, "yes")
        failure_updates = (
            (second, {"expires_at": timezone.now()}),
            (second, {"consented_at": None}),
            (second.participant, {"email": "korrigierte-adresse@example.invalid"}),
            (second.seat, {"status": BookingStatus.CANCELLED}),
            (case, {"status": RideCase.Status.CLOSED}),
        )
        for obj, updates in failure_updates:
            with self.subTest(updates=updates), transaction.atomic():
                type(obj).objects.filter(pk=obj.pk).update(**updates)
                listing = self.listing()
                self.assertContains(listing, "Kontaktfreigabe beendet")
                self.assertContains(listing, "2 zugesagt")
                self.assertNotContains(listing, "Kontakte freigegeben")
                self.assert_card_state(listing, "no-match" if obj == case else "awaiting-replies")
                transaction.set_rollback(True)

    def test_expired_unanswered_invitation_is_no_longer_open(self):
        case = self.make_case()
        first, second, third = self.start(case)
        self.reply(first, "yes")
        self.reply(second, "no")
        type(third).objects.filter(pk=third.pk).update(expires_at=timezone.now())
        listing = self.listing()
        self.assertContains(listing, "Zu wenig Zusagen")
        self.assert_card_state(listing, "no-match")
        self.assertContains(listing, "0 offen")
        self.assertContains(listing, "1 ohne gültigen Zugang")
        self.assertContains(listing, "1 zugesagt")
        self.assertContains(listing, "1 abgesagt")

        detail = self.client.get(reverse("ride_detail", args=[case.pk]), secure=True)
        self.assertContains(detail, "Testzugang beendet", count=1)
        self.assertNotContains(detail, "Einladung vorbereitet")
        self.assertContains(detail, "Testfragebogen öffnen", count=2)

    def test_ordinary_card_omits_test_summary(self):
        self.make_case(is_test=False)
        listing = self.listing()
        self.assertContains(listing, "Details")
        self.assertContains(listing, '<article class="ride-card"')
        self.assertNotContains(listing, "Noch nicht gestartet")
        self.assertNotContains(listing, "Einladungen vorbereitet")

    def test_participation_queries_do_not_increase_per_card(self):
        self.start(self.make_case())
        with CaptureQueriesContext(connection) as one_case:
            self.listing()
        self.start(self.make_case(days=7))
        with CaptureQueriesContext(connection) as two_cases:
            self.listing()
        self.assertEqual(len(one_case), len(two_cases))
