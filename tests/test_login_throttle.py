from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings

from accounts.models import LoginAttemptBucket


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class LoginThrottleTests(TestCase):
    instant = datetime(2030, 5, 25, 10, 30, 24, tzinfo=UTC)
    login_data = {"username": "anmelde-testkonto", "password": "synthetisch-ungueltig"}

    def post(self, address="192.0.2.12", **kwargs):
        return self.client.post("/anmelden/", self.login_data, REMOTE_ADDR=address, **kwargs)

    def test_login_posts_from_one_address_are_limited_to_ten_per_minute(self):
        with patch("django.utils.timezone.now", return_value=self.instant):
            for _ in range(10):
                self.assertEqual(self.post().status_code, 200)

            response = self.post()
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers["Retry-After"], "36")
            self.assertIn("no-store", response.headers["Cache-Control"])
            self.assertEqual(self.client.get("/anmelden/").status_code, 200)
            self.assertEqual(self.post(address="192.0.2.13").status_code, 200)

        with patch("django.utils.timezone.now", return_value=self.instant + timedelta(seconds=36)):
            self.assertEqual(self.post().status_code, 200)

    @override_settings(HTTPS=False)
    def test_local_mode_ignores_untrusted_forwarding_headers(self):
        with patch("django.utils.timezone.now", return_value=self.instant):
            for request_number in range(10):
                response = self.post(headers={"X-Forwarded-For": f"198.51.100.{request_number + 1}"})
                self.assertEqual(response.status_code, 200)

            self.assertEqual(self.post(headers={"X-Forwarded-For": "198.51.100.25"}).status_code, 429)

    @override_settings(HTTPS=True)
    def test_https_proxy_uses_last_address_and_canonicalizes_ipv6(self):
        with patch("django.utils.timezone.now", return_value=self.instant):
            for request_number in range(10):
                response = self.post(secure=True, headers={
                    "X-Forwarded-For": f"198.51.100.{request_number + 1}, 2001:0db8:0:0:0:0:0:27",
                })
                self.assertEqual(response.status_code, 200)

            self.assertEqual(self.post(secure=True, headers={"X-Forwarded-For": "2001:db8::27"}).status_code, 429)
            self.assertEqual(self.post(secure=True, headers={"X-Forwarded-For": "2001:db8::28"}).status_code, 200)

    @override_settings(HTTPS=True)
    def test_invalid_proxy_header_uses_remote_address_instead_of_other_entries(self):
        with patch("django.utils.timezone.now", return_value=self.instant):
            for request_number in range(10):
                response = self.post(secure=True, headers={
                    "X-Forwarded-For": f"198.51.100.{request_number + 1}, invalid",
                })
                self.assertEqual(response.status_code, 200)

            self.assertEqual(self.post(secure=True).status_code, 429)

    def test_ipv4_mapped_address_shares_ipv4_bucket(self):
        with patch("django.utils.timezone.now", return_value=self.instant):
            for _ in range(10):
                self.assertEqual(self.post(address="::ffff:192.0.2.12").status_code, 200)

            self.assertEqual(self.post(address="192.0.2.12").status_code, 429)

    def test_login_keeps_csrf_protection_and_rejected_requests_do_not_consume_attempts(self):
        client = Client(enforce_csrf_checks=True)
        with patch("django.utils.timezone.now", return_value=self.instant):
            for _ in range(11):
                self.assertEqual(client.post("/anmelden/", self.login_data).status_code, 403)

            self.assertFalse(LoginAttemptBucket.objects.exists())
            client.get("/anmelden/")
            csrf_token = client.cookies["csrftoken"].value
            response = client.post("/anmelden/", self.login_data | {"csrfmiddlewaretoken": csrf_token})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(LoginAttemptBucket.objects.get().attempts, 1)

    def test_request_prunes_expired_buckets_in_bounded_batches_and_stores_only_a_digest(self):
        minute_start = self.instant.replace(second=0)
        expired_window = minute_start - timedelta(minutes=2)
        retained_window = minute_start - timedelta(minutes=1)
        LoginAttemptBucket.objects.bulk_create([
            LoginAttemptBucket(key=f"{index:064x}", window_start=expired_window, attempts=1)
            for index in range(130)
        ])
        LoginAttemptBucket.objects.create(key="f" * 64, window_start=retained_window, attempts=1)

        with patch("django.utils.timezone.now", return_value=self.instant):
            self.assertEqual(self.post().status_code, 200)

        self.assertEqual(LoginAttemptBucket.objects.filter(window_start=expired_window).count(), 66)
        self.assertTrue(LoginAttemptBucket.objects.filter(window_start=retained_window).exists())
        self.assertRegex(LoginAttemptBucket.objects.get(window_start=minute_start).key, r"^[a-f0-9]{64}$")


@skipUnless(connection.vendor == "postgresql", "Parallel login attempts require PostgreSQL.")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ConcurrentLoginThrottleTests(TransactionTestCase):
    def test_concurrent_workers_share_one_attempt_limit(self):
        attempts = 16
        start = Barrier(attempts)

        def post_login(_):
            try:
                start.wait(timeout=10)
                response = Client().post("/anmelden/", {
                    "username": "anmelde-testkonto", "password": "synthetisch-ungueltig",
                }, REMOTE_ADDR="192.0.2.15")
                return response.status_code
            finally:
                connections.close_all()

        with patch("django.utils.timezone.now", return_value=LoginThrottleTests.instant):
            with ThreadPoolExecutor(max_workers=attempts) as workers:
                statuses = Counter(workers.map(post_login, range(attempts)))

        self.assertEqual(statuses, {200: 10, 429: 6})
        self.assertEqual(LoginAttemptBucket.objects.get().attempts, 10)
