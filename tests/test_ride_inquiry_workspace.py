from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from bookings.models import BookingStatus, Course, CourseSeat, CourseSession, Participant, RideCase, RideConfiguration, School
from bookings.rides_matching import sync_ride_cases
from tests.workspace_client import WorkspaceTestCase


@override_settings(HTTPS=True, RIDE_PORTAL_HOST="mitfahren.bms-ab.de")
class RideInquiryWorkspaceTests(WorkspaceTestCase):
    def make_case(self, *, test_case=True, days=3):
        starts = timezone.localtime(timezone.now() + timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)
        course = Course.objects.create(name="Synthetischer Testlehrgang")
        session = CourseSession.objects.create(
            course=course, school=School.WUERZBURG, starts_at=starts, ends_at=starts + timedelta(days=2),
        )
        for index in range(2):
            person = Participant.objects.create(name=f"Fiktive Person {index}", email=f"pilot{index}@example.invalid")
            CourseSeat.objects.create(session=session, participant=person, status=BookingStatus.CONFIRMED, is_test=test_case)
        sync_ride_cases()
        case = RideCase.objects.get()
        case.is_test = test_case
        case.save(update_fields=["is_test"])
        return case

    def user_with(self, codename):
        user = get_user_model().objects.create_user(f"pilot-{codename}")
        user.user_permissions.add(Permission.objects.get(content_type__app_label="accounts", codename=codename))
        return user

    def test_header_automation_switch_is_admin_only_and_persisted(self):
        rides_url = reverse("rides")
        initial = self.client.get(rides_url, secure=True)
        self.assertContains(initial, 'role="switch" aria-checked="false"')
        automation_url = reverse("ride_automation")
        response = self.client.post(automation_url, {"enabled": "1", "next": f"{rides_url}?phase=ready"}, secure=True)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"{rides_url}?phase=ready")
        self.assertTrue(RideConfiguration.objects.get(pk=1).automatic_inquiries)
        self.assertContains(self.client.get(rides_url, secure=True), 'role="switch" aria-checked="true"')

        self.client.force_login(self.user_with("edit_rides"))
        self.assertNotContains(self.client.get(rides_url, secure=True), 'role="switch"')
        denied = self.client.post(automation_url, {"enabled": "0"}, secure=True)
        self.assertEqual(denied.status_code, 403)
        self.assertTrue(RideConfiguration.objects.get(pk=1).automatic_inquiries)

    def test_automation_endpoint_is_csrf_protected_and_return_stays_local(self):
        automation_url = reverse("ride_automation")
        browser = self.authenticated_client(enforce_csrf_checks=True)
        response = browser.post(automation_url, {"enabled": "1"}, secure=True)
        self.assertEqual(response.status_code, 403)

        response = self.client.post(automation_url, {"enabled": "1", "next": "//example.invalid/"}, secure=True)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("rides"))

    def test_manual_start_previews_and_states_are_available_only_to_editors(self):
        case = self.make_case()
        start_url = reverse("ride_inquiry_start", args=[case.pk])
        detail_url = reverse("ride_detail", args=[case.pk])
        listing = self.client.get(reverse("rides"), secure=True)
        self.assertContains(listing, "Testabfrage starten")

        self.client.force_login(self.user_with("edit_rides"))
        response = self.client.post(start_url, secure=True)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(case.participation_requests.count(), 2)
        reply = case.participation_requests.order_by("pk").first()
        reply.answer = "yes"
        reply.save(update_fields=["answer"])
        response = self.client.get(detail_url, secure=True)
        self.assertIn(b"Pers\xc3\xb6nlicher Testcode", response.content)
        self.assertIn(b"mitfahren.bms-ab.de", response.content)
        self.assertIn(b"Zugesagt", response.content)
        self.assertIn(b"Einladung vorbereitet", response.content)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertContains(self.client.get(reverse("rides"), secure=True), "<dt>Einladungen</dt><dd>2</dd>", html=True)
        self.assertNotContains(self.client.get(reverse("rides"), secure=True), "Persönlicher Testcode")

        self.client.force_login(self.user_with("view_rides"))
        reader_detail = self.client.get(detail_url, secure=True)
        self.assertEqual(reader_detail.status_code, 200)
        self.assertFalse(b"Pers\xc3\xb6nlicher Testcode" in reader_detail.content)
        self.assertEqual(self.client.post(start_url, secure=True).status_code, 403)
        self.assertNotContains(self.client.get(reverse("rides"), secure=True), "Testabfrage starten")

    def test_manual_start_is_unavailable_for_real_case_or_when_automation_is_on(self):
        case = self.make_case(test_case=False)
        url = reverse("ride_inquiry_start", args=[case.pk])
        self.assertNotContains(self.client.get(reverse("rides"), secure=True), "Testabfrage starten")
        self.assertEqual(self.client.post(url, secure=True).status_code, 404)
        self.assertFalse(case.participation_requests.exists())

        case.is_test = True
        case.save(update_fields=["is_test"])
        self.client.post(reverse("ride_automation"), {"enabled": "1"}, secure=True)
        self.assertNotContains(self.client.get(reverse("rides"), secure=True), "Testabfrage starten")
