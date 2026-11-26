from datetime import UTC, datetime
from html import escape

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse

from bookings.models import BookingHistory, BookingStatus, Course, CourseSeat, CourseSession, Participant, School
from tests.workspace_client import WorkspaceTestCase


class BookingDeleteViewTests(WorkspaceTestCase):
    @classmethod
    def setUpTestData(cls):
        course = Course.objects.create(name="Synthetischer Prüflehrgang")
        cls.session = CourseSession.objects.create(
            course=course, school=School.REGENSBURG,
            starts_at=datetime(2031, 6, 2, 7, tzinfo=UTC), ends_at=datetime(2031, 6, 2, 14, tzinfo=UTC),
        )
        cls.person = Participant.objects.create(name="Mara Testperson")
        cls.target, cls.other = [
            CourseSeat.objects.create(
                session=cls.session, participant=cls.person, status=BookingStatus.CONFIRMED,
                external_reference=reference,
            )
            for reference in ("TEST-LOESCHEN", "TEST-BEHALTEN")
        ]
        for seat in (cls.target, cls.other):
            BookingHistory.objects.create(seat=seat, participant=cls.person, status=seat.status)

    def delete_url(self, seat=None, **params):
        return reverse("booking_delete", args=[(seat or self.target).pk], query=params)

    def login_with(self, *codenames):
        user = get_user_model().objects.create_user("synthetic-booking-editor")
        user.user_permissions.add(*Permission.objects.filter(content_type__app_label="accounts", codename__in=codenames))
        self.client.force_login(user)

    def payload(self, *, url=None, client=None):
        response = (client or self.client).get(url or self.delete_url())
        self.assertEqual(response.status_code, 200)
        return {"confirm_id": str(self.target.pk), "snapshot": response.context["form"]["snapshot"].value()}

    def test_confirmation_is_read_only_and_identifies_one_booking_with_contextual_cancel(self):
        navigation = {"q": "Mara Testperson", "school": "regensburg", "status": "confirmed", "list_page": 2}
        delete_url = self.delete_url(**navigation)
        detail_url = reverse("booking_detail", args=[self.target.pk], query=navigation)
        list_url = reverse("bookings", query={"q": "Mara Testperson", "school": "regensburg", "status": "confirmed", "page": 2})
        detail = self.client.get(detail_url)
        self.assertContains(detail, f'href="{escape(delete_url)}"')

        confirmation = self.client.get(delete_url)

        self.assertContains(confirmation, f"Buchungs-ID {self.target.pk}")
        self.assertContains(confirmation, self.session.course.name)
        self.assertContains(confirmation, self.person.name)
        self.assertContains(confirmation, "SFS Regensburg")
        self.assertContains(confirmation, "Bestätigt")
        self.assertContains(confirmation, "02.06.2031")
        self.assertContains(confirmation, f'href="{escape(detail_url)}"')
        self.assertContains(confirmation, f'href="{escape(list_url)}"')
        self.assertContains(confirmation, f'action="{escape(delete_url)}"')
        self.assertContains(confirmation, 'name="csrfmiddlewaretoken"')
        self.assertNotContains(confirmation, 'data-live-region="main"')
        self.assertNotContains(confirmation, self.other.external_reference)
        self.assertIn("no-store", confirmation.headers["Cache-Control"])
        self.assertEqual(self.client.head(delete_url).status_code, 200)
        self.assertTrue(CourseSeat.objects.filter(pk=self.target.pk).exists())

    def test_booking_viewer_can_neither_start_nor_submit_deletion(self):
        payload = self.payload()
        self.login_with("view_bookings")

        detail = self.client.get(reverse("booking_detail", args=[self.target.pk]))
        self.assertNotContains(detail, self.delete_url())
        self.assertEqual(self.client.get(self.delete_url()).status_code, 403)
        self.assertEqual(self.client.post(self.delete_url(), payload).status_code, 403)
        self.assertTrue(CourseSeat.objects.filter(pk=self.target.pk).exists())

    def test_post_requires_csrf_and_the_exact_ascii_booking_id(self):
        csrf_client = self.authenticated_client(enforce_csrf_checks=True)
        payload = self.payload(client=csrf_client)
        self.assertEqual(csrf_client.post(self.delete_url(), payload).status_code, 403)
        payload["csrfmiddlewaretoken"] = csrf_client.cookies["csrftoken"].value
        target_id = str(self.target.pk)
        wrong_values = (
            str(self.other.pk), f"0{target_id}", f"{target_id} ",
            target_id.translate(str.maketrans("0123456789", "０１２３４５６７８９")),
        )
        for wrong_id in wrong_values:
            with self.subTest(wrong_id=wrong_id):
                response = csrf_client.post(self.delete_url(), {**payload, "confirm_id": wrong_id})
                self.assertEqual(response.status_code, 400)
                self.assertIn("confirm_id", response.context["form"].errors)
        incomplete = csrf_client.post(self.delete_url(), {**payload, "snapshot": ""})
        self.assertContains(incomplete, "Die Bestätigung ist unvollständig.", status_code=400)
        self.assertFalse(incomplete.context["form"]["confirm_id"].value())
        self.assertEqual(CourseSeat.objects.filter(pk__in=[self.target.pk, self.other.pk]).count(), 2)

    def test_stale_confirmation_displays_current_state_and_requires_a_new_confirmation(self):
        payload = self.payload()
        self.target.status = BookingStatus.CANCELLED
        self.target.save(update_fields=["status"])

        stale = self.client.post(self.delete_url(), payload)

        self.assertContains(stale, "Die Buchung wurde seit dem Öffnen geändert.", status_code=409)
        self.assertContains(stale, "Storniert", status_code=409)
        self.assertFalse(stale.context["form"]["confirm_id"].value())
        updated_snapshot = stale.context["form"]["snapshot"].value()
        self.assertNotEqual(updated_snapshot, payload["snapshot"])
        self.assertTrue(CourseSeat.objects.filter(pk=self.target.pk).exists())
        confirmed = self.client.post(self.delete_url(), {**payload, "snapshot": updated_snapshot})
        self.assertRedirects(confirmed, reverse("bookings"), fetch_redirect_response=False)

    def test_edit_permission_can_delete_only_the_selected_booking_and_see_specific_feedback(self):
        self.login_with("edit_bookings")
        url = self.delete_url(q="Mara Testperson", school="regensburg", status="confirmed", list_page=2, next="https://example.invalid/")
        payload = self.payload(url=url)
        payload["next"] = "https://example.invalid/"

        result = self.client.post(url, payload)

        list_url = reverse("bookings", query={"q": "Mara Testperson", "school": "regensburg", "status": "confirmed", "page": 2})
        self.assertRedirects(result, list_url, fetch_redirect_response=False)
        self.assertFalse(CourseSeat.objects.filter(pk=self.target.pk).exists())
        self.assertFalse(BookingHistory.objects.filter(seat_id=self.target.pk).exists())
        self.assertTrue(CourseSeat.objects.filter(pk=self.other.pk, participant=self.person).exists())
        self.assertTrue(BookingHistory.objects.filter(seat=self.other).exists())
        self.assertEqual(self.client.post(url, payload).status_code, 404)
        self.assertNotContains(self.client.get(list_url, HTTP_X_BMS_LIVE="1"), "wurde endgültig gelöscht")
        self.assertContains(self.client.get(list_url), f"Buchung {self.target.pk} wurde endgültig gelöscht.")
        self.assertNotContains(self.client.get(list_url), "wurde endgültig gelöscht")

    def test_unassigned_booking_can_be_deleted_using_its_ungrouped_id(self):
        empty = CourseSeat.objects.create(pk=12001, session=self.session)
        url = self.delete_url(empty)
        confirmation = self.client.get(url)

        self.assertContains(confirmation, "Buchungs-ID 12001")
        self.assertContains(confirmation, "Keine Person zugeordnet")
        self.assertNotContains(confirmation, "12.001")
        result = self.client.post(url, {"confirm_id": "12001", "snapshot": confirmation.context["form"]["snapshot"].value()})

        self.assertRedirects(result, reverse("bookings"), fetch_redirect_response=False)
        self.assertFalse(CourseSeat.objects.filter(pk=12001).exists())
