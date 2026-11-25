from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import Client, TestCase, override_settings


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
        rights = {
            f"access_{section}": "none"
            for section in (
                "overview", "bookings", "calendar", "rides", "brigades", "review", "preview", "mailbox", "mail_receipts",
            )
        }
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

    def test_admin_creates_account_with_only_selected_workspace_rights(self):
        self.client.force_login(self.admin)

        response = self.client.post("/benutzer/neu/", self.rights(bookings="edit", calendar="view") | {
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
            "view_bookings", "edit_bookings", "view_calendar",
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

    def test_post_without_csrf_does_not_disable_account(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)

        response = client.post(f"/benutzer/{self.member.pk}/", self.rights())

        self.assertEqual(response.status_code, 403)
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
