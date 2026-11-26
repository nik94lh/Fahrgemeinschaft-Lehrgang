from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from bookings.models import (
    BookingHistory, BookingStatus, Carpool, Course, CourseSeat, CourseSession, EmailEvent, FireBrigade,
    Participant, RideCase, RideSimulationInvitation, School,
)
from mailimport.models import MailReceipt
from tests.workspace_client import WorkspaceTestCase


class StatisticsTests(WorkspaceTestCase):
    url = "/statistik/"

    @classmethod
    def setUpTestData(cls):
        course = Course.objects.create(name="Musterlehrgang A")
        online_course = Course.objects.create(name="Musterlehrgang B")
        cls.add_session(course, School.WUERZBURG, datetime(2031, 4, 30, 21, 59, tzinfo=UTC))
        may = cls.add_session(course, School.WUERZBURG, datetime(2031, 4, 30, 22, tzinfo=UTC))
        online = cls.add_session(online_course, School.WUERZBURG, datetime(2031, 5, 10, 12, tzinfo=UTC), online=True)
        cls.add_session(online_course, School.ONLINE, datetime(2031, 5, 11, 12, tzinfo=UTC))
        cls.add_session(course, School.REGENSBURG, datetime(2031, 5, 13, 12, tzinfo=UTC))
        june = cls.add_session(course, School.WUERZBURG, datetime(2031, 5, 31, 22, tzinfo=UTC))

        brigade = FireBrigade.objects.create(name="Synthetische Heimatwehr", municipality="Beispielort")
        other_brigade = FireBrigade.objects.create(name="Andere Musterwehr", municipality="Musterort")
        person = Participant.objects.create(name="Pat Privatmuster", email="pat@example.invalid", home_brigade=brigade)
        second = Participant.objects.create(name="Jana Musterperson", home_brigade=brigade)
        cancelled = Participant.objects.create(
            name="Robin Stornomuster", email="robin@example.invalid", home_brigade=other_brigade,
        )
        cls.seat = CourseSeat.objects.create(
            session=may, participant=person, status=BookingStatus.CONFIRMED, external_reference="REF-PRIVATMUSTER",
        )
        BookingHistory.objects.create(seat=cls.seat, participant=person, status=BookingStatus.APPROVED)
        CourseSeat.objects.create(session=may, participant=person, status=BookingStatus.AWAITING_CONFIRMATION)
        CourseSeat.objects.create(session=may, participant=cancelled, status=BookingStatus.CANCELLED)
        CourseSeat.objects.create(session=may, status=BookingStatus.NEEDS_REVIEW)
        CourseSeat.objects.create(session=online, participant=person, status=BookingStatus.APPROVED)
        CourseSeat.objects.create(session=online, participant=second, status=BookingStatus.CONFIRMED)
        CourseSeat.objects.create(session=june, participant=second, status=BookingStatus.CONFIRMED)

    @staticmethod
    def add_session(course, school, starts_at, *, online=False):
        return CourseSession.objects.create(
            course=course, school=school, is_online=online, starts_at=starts_at, ends_at=starts_at + timedelta(hours=2),
        )

    def sign_in(self, *codenames):
        user = get_user_model().objects.create_user(f"statistik-{len(codenames)}-{codenames[-1]}")
        permissions = Permission.objects.filter(content_type__app_label="accounts", codename__in=codenames)
        self.assertEqual(permissions.count(), len(codenames))
        user.user_permissions.add(*permissions)
        self.client.force_login(user)

    def test_berlin_month_counts_current_state_and_distinct_people(self):
        with timezone.override("UTC"):
            response = self.client.get(self.url, {"year": "2031", "month": "05", "school": School.WUERZBURG})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["period_start"], date(2031, 5, 1))
        self.assertEqual(response.context["period_end"], date(2031, 5, 31))
        self.assertEqual(response.context["metrics"], {
            "courses": 2, "sessions": 2, "bookings": 6, "people": 2, "people_with_email": 1,
            "brigades": 1, "online_sessions": 1,
        })
        self.assertEqual({row["status"]: row["count"] for row in response.context["status_counts"]}, {
            BookingStatus.APPROVED: 1, BookingStatus.AWAITING_CONFIRMATION: 1, BookingStatus.CONFIRMED: 2,
            BookingStatus.REASSIGNED: 0, BookingStatus.CANCELLED: 1, BookingStatus.COMPLETED: 0,
            BookingStatus.NEEDS_REVIEW: 1,
        })
        self.assertEqual({row["school"]: row["sessions"] for row in response.context["school_rows"]}, {School.WUERZBURG: 2})
        self.assertEqual({row["label"]: row["bookings"] for row in response.context["course_rows"]}, {
            "Musterlehrgang A": 4, "Musterlehrgang B": 2,
        })
        self.assertEqual([(row["sessions"], row["bookings"]) for row in response.context["trend"]], [
            (0, 0), (0, 0), (0, 0), (1, 0), (2, 6), (1, 1), (0, 0), (0, 0), (0, 0), (0, 0), (0, 0), (0, 0),
        ])
        self.assertEqual(response.context["trend_max"], 6)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_online_filter_counts_online_terms_with_and_without_an_organizing_school(self):
        response = self.client.get(self.url, {"year": "2031", "month": "5", "school": School.ONLINE})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["metrics"]["sessions"], 2)
        self.assertEqual(response.context["metrics"]["online_sessions"], 2)
        self.assertEqual({row["school"] for row in response.context["school_rows"]}, {School.ONLINE, School.WUERZBURG})
        self.assertEqual(response.context["ride_counts"], {"total": 0, "active": 0, "closed": 0})

    def test_invalid_filters_fall_back_to_the_current_berlin_year_and_zero_totals(self):
        with patch("bookings.statistics.timezone", wraps=timezone) as clock:
            clock.now.return_value = datetime(2030, 12, 31, 23, 30, tzinfo=UTC)
            response = self.client.get(self.url, {"year": "1880", "month": "99", "school": "andere"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.context["year"], response.context["month"], response.context["school"]), (2031, None, ""))
        self.assertEqual(response.context["period_start"], date(2031, 1, 1))
        self.assertEqual(response.context["period_end"], date(2031, 12, 31))
        empty = self.client.get(self.url, {"year": "9998", "month": "12"})
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.context["period_end"], date(9998, 12, 31))
        self.assertEqual(empty.context["metrics"]["bookings"], 0)
        self.assertTrue(all(row["count"] == 0 for row in empty.context["status_counts"]))
        self.assertEqual(empty.context["trend_max"], 1)

    def test_statistics_right_is_independent_and_omits_restricted_queries_and_pii(self):
        self.assertRedirects(Client().get(self.url), "/anmelden/?next=/statistik/", fetch_redirect_response=False)
        self.sign_in("view_bookings")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.sign_in("view_statistics")

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url, {"year": "2031", "month": "5"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["metrics"]["people"], 2)
        self.assertIsNone(response.context["mail_counts"])
        self.assertIsNone(response.context["review_count"])
        self.assertEqual(response.context["review_reasons"], [])
        self.assertNotIn(MailReceipt._meta.db_table, "\n".join(query["sql"] for query in queries))
        self.assertNotIn(EmailEvent._meta.db_table, "\n".join(query["sql"] for query in queries))
        for private in ("Pat Privatmuster", "pat@example.invalid", "Synthetische Heimatwehr", "REF-PRIVATMUSTER"):
            self.assertNotContains(response, private)

    @override_settings(HTTPS=True)
    def test_mail_intake_and_open_review_use_their_own_dates_across_schools(self):
        for uid, observed, outcome in (
            (1, datetime(2031, 4, 30, 22, tzinfo=UTC), MailReceipt.Outcome.APPLIED),
            (2, datetime(2031, 4, 30, 21, 59, tzinfo=UTC), MailReceipt.Outcome.SKIPPED),
        ):
            receipt = MailReceipt.objects.create(
                source_id="s" * 64, uid_validity=1, uid=uid,
                sent_at=datetime(2031, 5, 15, tzinfo=UTC), outcome=outcome,
            )
            MailReceipt.objects.filter(pk=receipt.pk).update(observed_at=observed)
        for offset, imported_at, state in (
            (1, datetime(2031, 4, 30, 22, tzinfo=UTC), EmailEvent.State.NEEDS_REVIEW),
            (2, datetime(2031, 4, 30, 21, 59, tzinfo=UTC), EmailEvent.State.NEEDS_REVIEW),
            (3, datetime(2031, 5, 12, tzinfo=UTC), EmailEvent.State.REVIEWED),
        ):
            event = EmailEvent.objects.create(
                sha256=str(offset) * 64, state=state, seat=self.seat,
                review_reason=EmailEvent.ReviewReason.REFERENCE_MISMATCH,
            )
            EmailEvent.objects.filter(pk=event.pk).update(imported_at=imported_at)
        filters = {"year": "2031", "month": "5", "school": School.REGENSBURG}

        self.sign_in("view_statistics", "view_mail_receipts")
        insecure = self.client.get(self.url, filters)
        self.assertIsNone(insecure.context["mail_counts"])
        self.assertIsNone(insecure.context["review_count"])
        inbox = self.client.get(self.url, filters, secure=True)
        self.assertIsNone(inbox.context["review_count"])
        self.assertEqual(inbox.context["review_reasons"], [])
        self.assertEqual(inbox.context["mail_counts"]["total"], 1)
        self.assertEqual({row["outcome"]: row["count"] for row in inbox.context["mail_counts"]["outcomes"]}, {
            MailReceipt.Outcome.APPLIED: 1, MailReceipt.Outcome.REVIEW: 0, MailReceipt.Outcome.DUPLICATE: 0,
            MailReceipt.Outcome.ADDRESS_ADDED: 0, MailReceipt.Outcome.SKIPPED: 0,
        })

        self.sign_in("view_statistics", "view_review")
        review = self.client.get(self.url, filters, secure=True)
        self.assertIsNone(review.context["mail_counts"])
        self.assertEqual(review.context["review_count"], 1)
        self.assertEqual(review.context["review_reasons"], [{
            "label": EmailEvent.ReviewReason.REFERENCE_MISMATCH.label, "count": 1,
        }])

    def test_ride_statistics_count_active_and_closed_roundtrips_without_simulation(self):
        fixtures = (
            (15, RideCase.Status.OPEN, True, RideCase.ROUND_TRIP),
            (16, RideCase.Status.CLOSED, False, RideCase.ROUND_TRIP),
            (17, RideCase.Status.OPEN, False, RideCase.ROUND_TRIP),
            (1, RideCase.Status.OPEN, True, RideCase.ROUND_TRIP),
            (18, RideCase.Status.CLOSED, False, Carpool.Direction.OUTBOUND),
        )
        for day, status, matches, direction in fixtures:
            outward = datetime(2031, 5, day, 12, tzinfo=UTC)
            returning = outward + timedelta(days=1) if direction == RideCase.ROUND_TRIP else None
            RideCase.objects.create(
                match_key=f"synthetic-{day}", school=School.WUERZBURG, direction=direction, status=status,
                currently_matches=matches, travel_date=outward.date(), earliest=outward, latest=outward,
                return_date=returning.date() if returning else None, return_earliest=returning, return_latest=returning,
            )
        RideSimulationInvitation.objects.create(
            owner=self.workspace_admin, respondent=1, token_digest="s" * 64, answered_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=1), interest=True,
        )

        with patch("bookings.statistics.timezone", wraps=timezone) as clock:
            clock.now.return_value = datetime(2031, 5, 10, tzinfo=UTC)
            response = self.client.get(self.url, {"year": "2031", "month": "5", "school": School.WUERZBURG})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["ride_counts"], {"total": 4, "active": 1, "closed": 1})
