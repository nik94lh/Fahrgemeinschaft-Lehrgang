from datetime import timedelta
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from bookings.models import (
    BookingStatus, Course, CourseSeat, CourseSession, Participant, RideCase, RideNotice, RideParticipation, School,
)
from bookings.ride_participation import invite_code, notice_preview, start_ride_inquiry
from bookings.rides_matching import sync_ride_cases


PORTAL = "/teilnahme/"
REPLY = f"{PORTAL}antwort/"
PORTAL_HOST = "mitfahren.example.test"
WORKSPACE_HOST = "verwaltung.example.test"
PORTAL_REQUEST = {"secure": True, "HTTP_HOST": PORTAL_HOST}


@override_settings(HTTPS=True, RIDE_PORTAL_HOST=PORTAL_HOST, ALLOWED_HOSTS=[PORTAL_HOST, WORKSPACE_HOST])
class RideParticipationHostTests(TestCase):
    def test_participant_host_serves_form_without_serving_workspace_routes(self):
        response = self.client.get(PORTAL, **PORTAL_REQUEST)
        self.assertContains(response, 'name="code"')
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(self.client.get(PORTAL, HTTP_HOST=PORTAL_HOST).status_code, 404)

        for path in ("/", "/anmelden/", "/konto/", "/buchungen/", "/fahrgemeinschaften/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, **PORTAL_REQUEST).status_code, 404)

        self.client.force_login(get_user_model().objects.create_superuser("synthetic-pilot-admin"))
        self.assertEqual(self.client.get("/buchungen/", **PORTAL_REQUEST).status_code, 404)

    def test_participation_routes_are_not_served_on_the_workspace_host(self):
        self.assertEqual(self.client.get(PORTAL, secure=True, HTTP_HOST=WORKSPACE_HOST).status_code, 404)
        response = self.client.post(REPLY, {"answer": "no"}, secure=True, HTTP_HOST=WORKSPACE_HOST)
        self.assertEqual(response.status_code, 404)

    def test_portal_posts_require_csrf_and_an_invitation_grant(self):
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(strict.post(PORTAL, {"code": "invalid"}, **PORTAL_REQUEST).status_code, 403)
        self.assertEqual(strict.post(REPLY, {"answer": "no"}, **PORTAL_REQUEST).status_code, 403)
        self.assertEqual(self.client.post(REPLY, {"answer": "no"}, **PORTAL_REQUEST).status_code, 403)


@override_settings(
    HTTPS=True,
    RIDE_PORTAL_HOST=PORTAL_HOST,
    ALLOWED_HOSTS=[PORTAL_HOST, WORKSPACE_HOST],
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class RideParticipationFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        course = Course.objects.create(name="Synthetischer Pilotlehrgang")
        starts = timezone.now() + timedelta(days=5)
        session = CourseSession.objects.create(
            course=course, school=School.WUERZBURG,
            starts_at=starts, ends_at=starts + timedelta(days=1),
        )
        cls.people = []
        for index in range(1, 6):
            participant = Participant.objects.create(
                name=f"Synthetische Person {index}", email=f"teilnahme{index}@example.invalid",
            )
            CourseSeat.objects.create(
                session=session, participant=participant,
                status=BookingStatus.CONFIRMED, is_test=index <= 3,
            )
            cls.people.append(participant)
        sync_ride_cases()
        cls.test_case = RideCase.objects.get(is_test=True)
        cls.ordinary_case = RideCase.objects.get(is_test=False)

    def start(self):
        with self.captureOnCommitCallbacks(execute=True):
            created = start_ride_inquiry(self.test_case.pk)
        self.assertEqual(created, 3)
        invitations = self.test_case.participation_requests.order_by("participant_id")
        return [(invitation, invite_code(invitation)) for invitation in invitations]

    def login(self, client, code):
        self.page(client)
        response = self.post(client, PORTAL, code=code)
        self.assertRedirects(response, PORTAL, fetch_redirect_response=False)

    def reply(self, client, answer, **fields):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(client, REPLY, answer=answer, **fields)
        self.assertRedirects(response, PORTAL, fetch_redirect_response=False)

    def post(self, client, path, **fields):
        return client.post(
            path, fields, HTTP_ORIGIN=f"https://{PORTAL_HOST}",
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value, **PORTAL_REQUEST,
        )

    def page(self, client):
        return client.get(PORTAL, **PORTAL_REQUEST)

    def assert_answers(self, *expected):
        self.assertEqual(
            list(self.test_case.participation_requests.order_by("participant_id").values_list("answer", flat=True)),
            list(expected),
        )

    def test_pilot_only_invites_test_bookings_into_local_outbox(self):
        invitations = self.start()

        self.assertEqual(list(self.test_case.seats.values_list("is_test", flat=True)), [True, True, True])
        self.assertEqual(list(self.ordinary_case.seats.values_list("is_test", flat=True)), [False, False])
        self.assertCountEqual([row.participant for row, _ in invitations], self.people[:3])
        self.assertEqual(len({code for _, code in invitations}), 3)
        self.assertEqual(len({row.token_digest for row, _ in invitations}), 3)
        for row, code in invitations:
            self.assertFalse(row.token_digest == code, "The access code must not be stored in clear text.")
            preview = notice_preview(row.notices.get(kind=RideNotice.Kind.INVITATION))
            link = urlsplit(preview["link"])
            self.assertEqual((link.scheme, link.hostname), ("https", PORTAL_HOST))
            self.assertTrue(link.path == PORTAL and not link.query, "Use the portal path without any query string.")
            self.assertTrue(link.fragment == code, "The access code belongs in the URL fragment.")
            self.assertEqual(preview["recipient"], row.participant.email)
        self.assertEqual(RideNotice.objects.filter(kind=RideNotice.Kind.INVITATION).count(), 3)

        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(start_ride_inquiry(self.test_case.pk), 0)
            with self.assertRaises(ValueError):
                start_ride_inquiry(self.ordinary_case.pk)
        self.assertEqual(self.test_case.participation_requests.count(), 3)
        self.assertEqual(RideNotice.objects.filter(kind=RideNotice.Kind.INVITATION).count(), 3)
        self.assertFalse(self.ordinary_case.participation_requests.exists())
        self.assertEqual(mail.outbox, [])

    def test_replies_share_selected_peer_contacts_and_withdraw_them(self):
        (_, first_code), (_, second_code), (_, third_code) = self.start()
        first, second, third = (Client(enforce_csrf_checks=True) for _ in range(3))
        first_person, second_person, third_person = self.people[:3]
        for client, code in ((first, first_code), (second, second_code), (third, third_code)):
            self.login(client, code)
        first_fields = {"channels": ["email"], "phone": "000000001", "consent": "on"}
        second_fields = {
            "channels": ["whatsapp", "call"], "phone": "000000002", "consent": "on",
            "brigade": "Synthetische Feuerwehr Süd",
        }

        without_consent = self.post(first, REPLY, answer="yes", channels=["email"])
        self.assertEqual(without_consent.status_code, 400)
        self.assert_answers("pending", "pending", "pending")

        self.reply(first, "yes", **first_fields)
        self.reply(first, "yes", **first_fields)
        self.assert_answers("yes", "pending", "pending")
        self.assertNotContains(self.page(first), second_person.name)
        self.assertFalse(RideNotice.objects.filter(kind=RideNotice.Kind.CONTACTS).exists())

        self.reply(second, "yes", **second_fields)
        self.reply(second, "yes", **second_fields)
        self.reply(third, "no")
        self.assert_answers("yes", "yes", "no")
        self.assertEqual(RideNotice.objects.filter(kind=RideNotice.Kind.CONTACTS).count(), 2)

        first_page = self.page(first)
        self.assertContains(first_page, second_person.name)
        self.assertContains(first_page, second_fields["phone"])
        self.assertContains(first_page, second_fields["brigade"])
        self.assertNotContains(first_page, second_person.email)
        self.assertNotContains(first_page, third_person.name)
        second_page = self.page(second)
        self.assertContains(second_page, first_person.email)
        self.assertNotContains(second_page, first_fields["phone"])
        self.assertNotContains(second_page, third_person.name)
        third_page = self.page(third)
        self.assertNotContains(third_page, first_person.email)
        self.assertNotContains(third_page, second_fields["phone"])

        reopened = Client(enforce_csrf_checks=True)
        self.login(reopened, second_code)
        self.assertContains(self.page(reopened), first_person.email)
        self.reply(reopened, "no")
        self.reply(reopened, "no")
        self.assert_answers("yes", "no", "no")
        withdrawals = RideNotice.objects.filter(kind=RideNotice.Kind.WITHDRAWAL)
        self.assertEqual(withdrawals.count(), 1)
        self.assertEqual(withdrawals.get().participation.participant, first_person)
        first_page = self.page(first)
        self.assertContains(first_page, "Eine Zusage wurde nachträglich zurückgezogen")
        self.assertNotContains(first_page, second_fields["phone"])
        self.assertNotContains(self.page(reopened), first_person.email)
        self.assertEqual(mail.outbox, [])

    def test_expiry_invalidates_the_code_and_an_existing_portal_grant(self):
        (entry, code), *_ = self.start()
        client = Client(enforce_csrf_checks=True)
        self.login(client, code)
        self.assertContains(self.page(client), entry.participant.name)

        RideParticipation.objects.filter(pk=entry.pk).update(expires_at=timezone.now() - timedelta(seconds=1))

        denied_reply = self.post(client, REPLY, answer="no")
        self.assertNotContains(denied_reply, entry.participant.name, status_code=403)
        denied_login = self.post(client, PORTAL, code=code)
        self.assertEqual(denied_login.status_code, 400)
        self.assertFalse(code.encode() in denied_login.content, "The invalid access code must not be echoed.")
        self.assert_answers("pending", "pending", "pending")

    def test_cancelling_a_booking_removes_contacts_and_invalidates_its_grant(self):
        (_, first_code), (second_entry, second_code), _ = self.start()
        first, second = (Client(enforce_csrf_checks=True) for _ in range(2))
        for client, code in ((first, first_code), (second, second_code)):
            self.login(client, code)
            self.reply(client, "yes", channels=["email"], consent="on")
        self.assertContains(self.page(first), second_entry.email)

        CourseSeat.objects.filter(pk=second_entry.seat_id).update(status=BookingStatus.CANCELLED)

        self.assertNotContains(self.page(first), second_entry.email)
        self.assertEqual(self.post(second, REPLY, answer="no").status_code, 403)
        self.assertEqual(self.post(second, PORTAL, code=second_code).status_code, 400)
