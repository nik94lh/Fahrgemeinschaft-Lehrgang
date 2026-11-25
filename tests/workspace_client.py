from django.contrib.auth import get_user_model
from django.test import Client, TestCase


class WorkspaceTestCase(TestCase):
    """Exercise workspace views with a real administrator session."""

    enforce_csrf_checks = False

    def setUp(self):
        super().setUp()
        self.workspace_admin = get_user_model().objects.create_user(
            username="workspace-admin", is_staff=True, is_superuser=True,
        )
        self.client = self.authenticated_client(enforce_csrf_checks=self.enforce_csrf_checks)

    def authenticated_client(self, *, enforce_csrf_checks=False):
        client = Client(enforce_csrf_checks=enforce_csrf_checks)
        client.force_login(self.workspace_admin)
        return client
