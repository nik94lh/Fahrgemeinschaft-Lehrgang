from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.sessions.middleware import SessionMiddleware
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from accounts.access import SECTIONS
from accounts.middleware import WorkspaceAccessMiddleware
from accounts.views import AccountPasswordChangeView


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AccountAdministrationTests(TestCase):
    fixtures_password = "synthetic-login-password-12"
    new_password = "synthetic-much-longer-password-73"

    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.admin = users.create_superuser("test-admin", password=cls.fixtures_password)
        cls.other_admin = users.create_superuser("other-test-admin", password=cls.fixtures_password)
        cls.member = users.create_user("test-member", password=cls.fixtures_password)

    def rights(self, **overrides):
        rights = {f"access_{section}": "none" for section in SECTIONS}
        return rights | {f"access_{section}": value for section, value in overrides.items()}

    def password_fields(self):
        return {"new_password1": self.new_password, "new_password2": self.new_password}

    def test_person_without_workspace_access_can_open_account(self):
        self.client.force_login(self.member)

        response = self.client.get("/konto/")

        self.assertContains(response, self.member.username)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_login_from_workspace_root_selects_first_available_tab(self):
        self.member.user_permissions.add(Permission.objects.get(
            content_type__app_label="accounts", codename="view_calendar",
        ))
        intercepted = self.client.get("/")
        self.assertRedirects(intercepted, "/anmelden/?next=/", fetch_redirect_response=False)

        response = self.client.post(intercepted.url, {
            "username": self.member.username, "password": self.fixtures_password, "next": "/",
        })

        self.assertRedirects(response, "/kalender/")

    def test_login_keeps_authorized_deep_link_and_blocks_external_redirect(self):
        self.member.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="accounts", codename__in=("view_overview", "view_calendar"),
        ))
        credentials = {"username": self.member.username, "password": self.fixtures_password}
        deep_link = "/kalender/?month=2030-02"

        response = self.client.post("/anmelden/", credentials | {"next": deep_link})
        self.assertRedirects(response, deep_link)
        response = self.client.post("/anmelden/", credentials | {"next": "https://external.invalid/"})
        self.assertRedirects(response, "/")

    def test_account_owner_can_change_password_and_keep_session(self):
        self.client.force_login(self.member)

        response = self.client.post("/konto/passwort/", {
            "old_password": self.fixtures_password, **self.password_fields(),
        })

        self.assertRedirects(response, "/konto/?passwort=1")
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(self.new_password))

    def test_ordinary_account_cannot_open_or_mutate_admin_pages(self):
        self.client.force_login(self.member)
        edit_url = f"/benutzer/{self.member.pk}/"
        reset_url = f"{edit_url}passwort/"

        for url in ("/benutzer/", "/benutzer/neu/", edit_url, reset_url):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
        denied = self.client.post(edit_url, self.rights(bookings="edit") | {"is_active": "on"})

        self.assertEqual(denied.status_code, 403)
        self.assertFalse(self.member.user_permissions.exists())

    def test_account_list_shows_last_login_in_berlin_time(self):
        self.client.force_login(self.admin)
        self.member.last_login = datetime(2030, 7, 11, 22, 42, tzinfo=UTC)
        self.member.save(update_fields=["last_login"])

        with timezone.override(UTC):
            response = self.client.get("/benutzer/")

        self.assertContains(response, '<th scope="col">Letzte Anmeldung</th>', html=True)
        self.assertContains(
            response,
            '<td data-label="Letzte Anmeldung"><time datetime="2030-07-12T00:42:00+02:00">'
            '12.07.2030, 00:42 Uhr</time></td>', html=True,
        )
        self.assertContains(response, '<td data-label="Letzte Anmeldung">Noch nie</td>', html=True)

    def test_admin_creates_account_with_only_selected_workspace_rights(self):
        self.client.force_login(self.admin)

        response = self.client.post("/benutzer/neu/", self.rights(bookings="edit", people="view", calendar="view") | {
            "username": "test-new-user", "password1": self.new_password, "password2": self.new_password,
            "is_superuser": "on", "is_staff": "on",
        })

        self.assertEqual(response.status_code, 302)
        created = get_user_model().objects.get(username="test-new-user")
        self.assertTrue(created.is_active)
        self.assertFalse(created.is_staff)
        self.assertFalse(created.is_superuser)
        self.assertNotEqual(created.password, self.new_password)
        self.assertTrue(created.check_password(self.new_password))
        self.assertSetEqual(set(created.user_permissions.values_list("codename", flat=True)), {
            "view_bookings", "edit_bookings", "view_people", "view_calendar",
        })

    def test_creation_form_renders_linked_username_help_and_labels_each_tab(self):
        self.client.force_login(self.admin)

        response = self.client.get("/benutzer/neu/")

        self.assertContains(response, 'aria-describedby="id_username_helptext"')
        self.assertContains(response, 'id="id_username_helptext"')
        self.assertContains(response, '<label for="id_access_rides">Fahrgemeinschaften:</label>', html=True)

    def test_invalid_rights_leave_existing_account_unchanged(self):
        self.client.force_login(self.admin)

        response = self.client.post(f"/benutzer/{self.member.pk}/", self.rights(bookings="invalid"))

        self.assertEqual(response.status_code, 400)
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertFalse(self.member.user_permissions.exists())

    def test_people_right_only_offers_viewing_and_rejects_edit_grants(self):
        self.client.force_login(self.admin)
        edit_url = f"/benutzer/{self.member.pk}/"

        form = self.client.get(edit_url).context["form"]
        self.assertEqual(list(form.fields["access_people"].choices), [("none", "Kein Zugriff"), ("view", "Ansehen")])
        self.assertFalse(Permission.objects.filter(content_type__app_label="accounts", codename="edit_people").exists())

        response = self.client.post(edit_url, self.rights(people="edit") | {"is_active": "on"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("access_people", response.context["form"].errors)
        self.assertFalse(self.member.user_permissions.exists())

    def test_statistics_grant_survives_a_form_opened_before_the_tab_existed(self):
        permission = Permission.objects.get(content_type__app_label="accounts", codename="view_statistics")
        self.member.user_permissions.add(permission)
        self.client.force_login(self.admin)
        edit_url = f"/benutzer/{self.member.pk}/"
        legacy_rights = self.rights()
        del legacy_rights["access_statistics"]

        response = self.client.post(edit_url, legacy_rights | {"is_active": "on"})
        self.assertRedirects(response, "/benutzer/?gespeichert=1")
        self.assertTrue(self.member.user_permissions.filter(pk=permission.pk).exists())

        response = self.client.post(edit_url, self.rights(statistics="none") | {"is_active": "on"})
        self.assertRedirects(response, "/benutzer/?gespeichert=1")
        self.assertFalse(self.member.user_permissions.filter(pk=permission.pk).exists())

    def test_edit_form_displays_and_replaces_direct_workspace_rights(self):
        self.client.force_login(self.admin)
        account_permissions = Permission.objects.filter(content_type__app_label="accounts")
        group = Group.objects.create(name="test-legacy-group")
        group.permissions.add(account_permissions.get(codename="edit_calendar"))
        self.member.groups.add(group)
        self.member.user_permissions.add(account_permissions.get(codename="edit_bookings"))
        edit_url = f"/benutzer/{self.member.pk}/"

        response = self.client.get(edit_url)
        self.assertEqual(response.context["form"]["access_bookings"].value(), "edit")
        self.assertEqual(response.context["form"]["access_calendar"].value(), "none")
        response = self.client.post(edit_url, self.rights(bookings="view") | {"is_active": "on"})

        self.assertEqual(response.status_code, 302)
        self.assertSetEqual(set(self.member.user_permissions.values_list("codename", flat=True)), {"view_bookings"})

    def test_admin_can_disable_and_reenable_normal_account(self):
        self.client.force_login(self.admin)
        edit_url = f"/benutzer/{self.member.pk}/"

        self.assertEqual(self.client.post(edit_url, self.rights()).status_code, 302)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)

        self.assertEqual(self.client.post(edit_url, self.rights() | {"is_active": "on"}).status_code, 302)
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)

    def test_reactivation_requires_new_login_but_preserves_the_existing_password(self):
        stale_worker = Client()
        stale_browser = Client()
        stale_worker.force_login(self.member)
        stale_browser.force_login(self.member)
        self.client.force_login(self.admin)
        edit_url = f"/benutzer/{self.member.pk}/"
        self.assertEqual(stale_browser.get("/konto/").status_code, 200)

        self.assertEqual(self.client.post(edit_url, self.rights()).status_code, 302)
        self.assertEqual(self.client.post(edit_url, self.rights() | {"is_active": "on"}).status_code, 302)

        self.assertEqual(stale_worker.get("/konto/", headers={"X-BMS-Live": "1"}).status_code, 401)
        self.assertNotContains(stale_browser.get("/anmelden/"), '>Mein Konto</summary>')
        self.assertRedirects(stale_browser.get("/konto/"), "/anmelden/?next=/konto/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/benutzer/").status_code, 200)
        new_login = stale_browser.post("/anmelden/", {
            "username": self.member.username, "password": self.fixtures_password,
        })
        self.assertRedirects(new_login, "/konto/")
        self.assertEqual(self.client.post(edit_url, self.rights()).status_code, 302)
        self.assertEqual(self.client.post(edit_url, self.rights() | {"is_active": "on"}).status_code, 302)
        self.assertRedirects(stale_browser.get("/konto/"), "/anmelden/?next=/konto/", fetch_redirect_response=False)

    def test_in_flight_password_change_cannot_reactivate_disabled_account(self):
        member_client = Client()
        member_client.force_login(self.member)
        self.client.force_login(self.admin)
        original = AccountPasswordChangeView.form_valid

        def deactivate_then_finish(view, form):
            self.assertEqual(self.client.post(f"/benutzer/{self.member.pk}/", self.rights()).status_code, 302)
            return original(view, form)

        with patch.object(AccountPasswordChangeView, "form_valid", deactivate_then_finish):
            response = member_client.post("/konto/passwort/", {
                "old_password": self.fixtures_password, **self.password_fields(),
            })

        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.assertTrue(self.member.check_password(self.fixtures_password))
        self.assertRedirects(response, "/anmelden/?next=/konto/passwort/", fetch_redirect_response=False)

    def test_reactivating_an_account_disabled_before_update_revokes_legacy_cookie(self):
        stale_browser = Client()
        stale_browser.force_login(self.member)
        get_user_model().objects.filter(pk=self.member.pk).update(is_active=False)
        self.client.force_login(self.admin)

        self.assertEqual(self.client.post(
            f"/benutzer/{self.member.pk}/", self.rights() | {"is_active": "on"},
        ).status_code, 302)
        self.assertRedirects(stale_browser.get("/konto/"), "/anmelden/?next=/konto/", fetch_redirect_response=False)

    def test_admin_password_reset_invalidates_sessions_for_normal_account(self):
        self.client.force_login(self.admin)
        member_session = Client()
        member_session.force_login(self.member)

        response = self.client.post(f"/benutzer/{self.member.pk}/passwort/", self.password_fields())

        self.assertEqual(response.status_code, 302)
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(self.new_password))
        self.assertRedirects(member_session.get("/konto/"), "/anmelden/?next=/konto/", fetch_redirect_response=False)

    def test_admin_cannot_modify_or_reset_another_admin_in_normal_account_ui(self):
        self.client.force_login(self.admin)

        for suffix, payload in (("", self.rights()), ("passwort/", self.password_fields())):
            with self.subTest(suffix=suffix):
                response = self.client.post(f"/benutzer/{self.other_admin.pk}/{suffix}", payload)
                self.assertEqual(response.status_code, 404)

        self.other_admin.refresh_from_db()
        self.assertTrue(self.other_admin.is_active)
        self.assertTrue(self.other_admin.check_password(self.fixtures_password))

    def test_admin_can_grant_admin_role_from_user_list(self):
        self.client.force_login(self.admin)
        role_url = f"/benutzer/{self.member.pk}/admin-recht/"
        member_browser = Client()
        member_browser.force_login(self.member)
        self.assertContains(self.client.get("/benutzer/"), f'href="{role_url}"')
        confirmation_page = self.client.get(role_url)
        self.assertContains(confirmation_page, "Admin-Recht vergeben")
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_superuser)

        response = self.client.post(role_url, confirmation_page.context["form"].initial)

        self.assertRedirects(response, "/benutzer/?rolle=1")
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_superuser)
        self.assertFalse(self.member.is_staff)
        self.assertRedirects(
            member_browser.get("/benutzer/"), "/anmelden/?next=/benutzer/", fetch_redirect_response=False,
        )
        self.assertRedirects(member_browser.post("/anmelden/", {
            "username": self.member.username, "password": self.fixtures_password, "next": "/benutzer/",
        }), "/benutzer/")
        self.assertContains(self.client.get(role_url), "Admin-Recht entziehen")

    def test_admin_role_removal_keeps_tab_rights_and_revokes_sessions(self):
        self.client.force_login(self.admin)
        permission = Permission.objects.get(content_type__app_label="accounts", codename="view_bookings")
        self.other_admin.user_permissions.add(permission)
        previous_session = Client()
        previous_session.force_login(self.other_admin)
        role_url = f"/benutzer/{self.other_admin.pk}/admin-recht/"
        confirmation = self.client.get(role_url).context["form"].initial

        self.assertRedirects(self.client.post(role_url, confirmation), "/benutzer/?rolle=1")
        self.assertRedirects(self.client.post(role_url, confirmation), "/benutzer/?rolle=1")
        self.other_admin.refresh_from_db()
        self.assertFalse(self.other_admin.is_superuser)
        self.assertTrue(self.other_admin.is_staff)
        self.assertTrue(self.other_admin.user_permissions.filter(pk=permission.pk).exists())
        self.assertRedirects(
            previous_session.get("/konto/"), "/anmelden/?next=/konto/", fetch_redirect_response=False,
        )
        self.assertRedirects(previous_session.post("/anmelden/", {
            "username": self.other_admin.username, "password": self.fixtures_password,
        }), "/buchungen/")
        self.assertEqual(previous_session.get("/benutzer/").status_code, 403)

    def test_ordinary_user_cannot_grant_admin_role(self):
        self.client.force_login(self.member)
        role_url = f"/benutzer/{self.member.pk}/admin-recht/"

        self.assertEqual(self.client.get(role_url).status_code, 403)
        self.assertEqual(self.client.post(role_url, {"action": "grant"}).status_code, 403)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_superuser)

    def test_admin_cannot_remove_own_admin_role(self):
        self.client.force_login(self.admin)
        role_url = f"/benutzer/{self.admin.pk}/admin-recht/"
        self.assertNotContains(self.client.get("/benutzer/"), role_url)

        self.assertEqual(self.client.post(role_url, {"action": "revoke"}).status_code, 404)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_superuser)

    def test_admin_role_change_requires_valid_action_and_csrf(self):
        role_url = f"/benutzer/{self.member.pk}/admin-recht/"
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        confirmation_page = client.get(role_url)
        self.assertEqual(confirmation_page.status_code, 200)
        confirmation = confirmation_page.context["form"].initial
        self.assertEqual(client.post(role_url, confirmation).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(role_url, confirmation | {"action": "toggle"}).status_code, 400)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_superuser)

    def test_admin_role_cannot_be_granted_to_an_inactive_account(self):
        self.member.is_active = False
        self.member.save(update_fields=["is_active"])
        self.client.force_login(self.admin)

        role_url = f"/benutzer/{self.member.pk}/admin-recht/"
        response = self.client.post(role_url, self.client.get(role_url).context["form"].initial)

        self.assertContains(response, "Aktiviere das Konto", status_code=400)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_superuser)

    def test_old_confirmation_cannot_override_intervening_admin_role_changes(self):
        self.client.force_login(self.admin)
        role_url = f"/benutzer/{self.member.pk}/admin-recht/"
        old_confirmation = self.client.get(role_url).context["form"].initial
        self.assertEqual(self.client.post(role_url, old_confirmation).status_code, 302)
        current_confirmation = self.client.get(role_url).context["form"].initial
        self.assertEqual(self.client.post(role_url, current_confirmation).status_code, 302)

        response = self.client.post(role_url, old_confirmation)

        self.assertContains(response, "Das Konto wurde zwischenzeitlich geändert", status_code=409)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_superuser)

    def test_post_without_csrf_does_not_disable_account(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)

        response = client.post(f"/benutzer/{self.member.pk}/", self.rights())

        self.assertEqual(response.status_code, 403)
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)


@skipUnless(connection.vendor == "postgresql", "Concurrent account sessions require PostgreSQL.")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ConcurrentAccountRevocationTests(TransactionTestCase):
    def test_admins_cannot_remove_each_others_admin_role_concurrently(self):
        users = get_user_model().objects
        first, second = (users.create_superuser(username) for username in ("first-admin", "second-admin"))
        ready = Barrier(2, timeout=8)
        original = WorkspaceAccessMiddleware.process_view

        def synchronize_authorized_requests(middleware, request, view, view_args, view_kwargs):
            response = original(middleware, request, view, view_args, view_kwargs)
            if response is None and request.method == "POST" and request.resolver_match.url_name == "account_admin_role":
                ready.wait()
            return response

        def revoke(client, role_url, confirmation):
            try:
                return client.post(role_url, confirmation).status_code
            finally:
                connections.close_all()

        # The test client's template context is captured through a global signal.
        changes = []
        for actor, target in ((first, second), (second, first)):
            client = Client()
            client.force_login(actor)
            role_url = f"/benutzer/{target.pk}/admin-recht/"
            changes.append((client, role_url, client.get(role_url).context["form"].initial))

        with patch.object(WorkspaceAccessMiddleware, "process_view", synchronize_authorized_requests):
            with ThreadPoolExecutor(max_workers=2) as executor:
                votes = [executor.submit(revoke, *change) for change in changes]
                self.assertEqual(sorted(vote.result(timeout=12) for vote in votes), [302, 403])
        self.assertEqual(users.filter(is_active=True, is_superuser=True).count(), 1)

    def test_session_written_after_revocation_stays_invalid_if_login_was_already_complete(self):
        users = get_user_model().objects
        admin = users.create_superuser("test-admin")
        member = users.create_user("test-member", password="synthetic-login-race-password-12")
        self.client.force_login(admin)
        ready_to_write, allow_write = Event(), Event()
        original = SessionMiddleware.process_response

        def pause_session_write(middleware, request, response):
            if request.path == "/anmelden/" and response.status_code == 302:
                ready_to_write.set()
                if not allow_write.wait(8):
                    raise TimeoutError("Timed out waiting for account revocation.")
            return original(middleware, request, response)

        def sign_in():
            client = Client()
            try:
                response = client.post("/anmelden/", {
                    "username": member.username, "password": "synthetic-login-race-password-12",
                })
                return client, response
            finally:
                connections.close_all()

        rights = {f"access_{section}": "none" for section in SECTIONS}
        edit_url = f"/benutzer/{member.pk}/"
        with patch.object(SessionMiddleware, "process_response", pause_session_write):
            with ThreadPoolExecutor(max_workers=1) as executor:
                login = executor.submit(sign_in)
                try:
                    self.assertTrue(ready_to_write.wait(8))
                    self.assertEqual(self.client.post(edit_url, rights).status_code, 302)
                    self.assertEqual(self.client.post(edit_url, rights | {"is_active": "on"}).status_code, 302)
                finally:
                    allow_write.set()
                stale_browser, response = login.result(timeout=8)

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(stale_browser.get("/konto/"), "/anmelden/?next=/konto/", fetch_redirect_response=False)
