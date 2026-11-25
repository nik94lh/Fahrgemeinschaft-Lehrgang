from django.urls import reverse

from tests.workspace_client import WorkspaceTestCase


class ListingControlsTests(WorkspaceTestCase):
    def test_booking_filters_can_be_cleared_in_one_step(self):
        url = reverse("bookings")
        reset_link = f'<a class="button-link list-clear" href="{url}">Filter zurücksetzen</a>'

        filtered = self.client.get(url, {"school": "regensburg", "status": "confirmed", "q": "Übung"})

        self.assertContains(filtered, reset_link, html=True)
        self.assertContains(filtered, '<option value="regensburg" selected>SFS Regensburg</option>', html=True)
        self.assertContains(filtered, '<option value="confirmed" selected>Bestätigt</option>', html=True)
        self.assertContains(filtered, 'value="Übung"')
        self.assertNotContains(self.client.get(url), reset_link, html=True)

    def test_people_search_can_be_cleared_even_without_results(self):
        url = reverse("people")
        reset_link = f'<a class="button-link list-clear" href="{url}">Suche zurücksetzen</a>'

        searched = self.client.get(url, {"q": "Synthetischer Suchbegriff"})

        self.assertContains(searched, reset_link, html=True)
        self.assertContains(searched, "Keine Personen gefunden.")
        self.assertNotContains(self.client.get(url), reset_link, html=True)
