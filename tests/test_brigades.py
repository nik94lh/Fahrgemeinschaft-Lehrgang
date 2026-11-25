from decimal import Decimal

from bookings.models import FireBrigade
from tests.workspace_client import WorkspaceTestCase


class BrigadeRegisterTests(WorkspaceTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.existing = FireBrigade.objects.create(name="Musterwehr", municipality="Testgemeinde")
        FireBrigade.objects.create(name="Zweite Übungswehr", municipality="Beispielort", is_active=False)

    def form_data(self, **overrides):
        return {
            "name": "Übungswehr Alpha",
            "municipality": "Synthetischer Ort",
            "district": "Nord",
            "address": "Teststraße 7",
            "latitude": "49.800100",
            "longitude": "9.900100",
            "is_active": "on",
            **overrides,
        }

    def test_list_allows_search_by_municipality(self):
        response = self.client.get("/feuerwehren/", {"q": "testgemeinde"})

        self.assertContains(response, "Musterwehr")
        self.assertNotContains(response, "Zweite Übungswehr")
        self.assertContains(response, 'href="/feuerwehren/neu/"')
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_create_stores_the_coordinates_and_allows_editing(self):
        created = self.client.post("/feuerwehren/neu/", self.form_data())

        self.assertRedirects(created, "/feuerwehren/")
        brigade = FireBrigade.objects.get(name="Übungswehr Alpha")
        self.assertEqual(brigade.latitude, Decimal("49.800100"))
        self.assertEqual(brigade.longitude, Decimal("9.900100"))

        updated = self.client.post(
            f"/feuerwehren/{brigade.pk}/", self.form_data(is_active="", latitude="", longitude="")
        )

        self.assertRedirects(updated, "/feuerwehren/")
        brigade.refresh_from_db()
        self.assertFalse(brigade.is_active)
        self.assertIsNone(brigade.latitude)
        self.assertIsNone(brigade.longitude)

    def test_form_rejects_an_incomplete_or_out_of_range_coordinate_pair(self):
        for latitude, longitude in [("49.8", ""), ("91", "9.9"), ("49.8", "181")]:
            with self.subTest(latitude=latitude, longitude=longitude):
                response = self.client.post(
                    "/feuerwehren/neu/", self.form_data(latitude=latitude, longitude=longitude)
                )
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.context["form"].errors)
        self.assertEqual(FireBrigade.objects.count(), 2)

    def test_write_requires_csrf(self):
        response = self.authenticated_client(enforce_csrf_checks=True).post("/feuerwehren/neu/", self.form_data())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(FireBrigade.objects.count(), 2)
