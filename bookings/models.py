from django.core.validators import RegexValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class School(models.TextChoices):
    WUERZBURG = "wuerzburg", "SFS Würzburg"
    REGENSBURG = "regensburg", "SFS Regensburg"
    GERETSRIED = "geretsried", "SFS Geretsried"


class BookingStatus(models.TextChoices):
    APPROVED = "approved", "Genehmigt"
    AWAITING_CONFIRMATION = "awaiting_confirmation", "Bestätigung ausstehend"
    CONFIRMED = "confirmed", "Bestätigt"
    REASSIGNED = "reassigned", "Umgemeldet"
    CANCELLED = "cancelled", "Storniert"
    COMPLETED = "completed", "Abgeschlossen"
    NEEDS_REVIEW = "needs_review", "Klärungsbedarf"


class FireBrigade(models.Model):
    name = models.CharField(max_length=160)
    municipality = models.CharField(max_length=120)
    district = models.CharField(max_length=120, blank=True)
    address = models.CharField(max_length=255, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(latitude__isnull=True, longitude__isnull=True)
                | Q(
                    latitude__isnull=False,
                    longitude__isnull=False,
                    latitude__gte=-90,
                    latitude__lte=90,
                    longitude__gte=-180,
                    longitude__lte=180,
                ),
                name="bookings_fire_brigade_valid_coordinates",
            ),
        ]

    def __str__(self):
        return self.name


class Participant(models.Model):
    name = models.CharField(max_length=200)
    home_brigade = models.ForeignKey(
        FireBrigade,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="participants",
    )


class Course(models.Model):
    name = models.CharField(max_length=200)

    def __str__(self):
        return self.name


class CourseSession(models.Model):
    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name="sessions")
    school = models.CharField(max_length=16, choices=School.choices)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=F("starts_at")),
                name="bookings_course_session_end_after_start",
            ),
        ]
        indexes = [models.Index(fields=["school", "starts_at"], name="bookings_session_school_time")]


class CourseSeat(models.Model):
    session = models.ForeignKey(CourseSession, on_delete=models.PROTECT, related_name="seats")
    participant = models.ForeignKey(
        Participant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="seats",
    )
    external_reference = models.CharField(max_length=120, blank=True)
    status = models.CharField(
        max_length=24,
        choices=BookingStatus.choices,
        default=BookingStatus.NEEDS_REVIEW,
    )
    confirmation_deadline = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["external_reference"],
                condition=~Q(external_reference=""),
                name="bookings_seat_unique_external_ref",
            ),
        ]


class EmailEvent(models.Model):
    class Kind(models.TextChoices):
        APPROVAL = "approval", "Genehmigung"
        INVITATION = "invitation", "Einladung"
        CONFIRMATION_REQUEST = "confirmation_request", "Bestätigungsaufforderung"
        CONFIRMATION = "confirmation", "Teilnahmebestätigung"
        REASSIGNMENT = "reassignment", "Ummeldung"
        CANCELLATION = "cancellation", "Stornierung"
        AUTOMATIC_CANCELLATION = "automatic_cancellation", "Automatische Stornierung"
        SCHEDULE_CHANGE = "schedule_change", "Terminänderung"
        REMINDER = "reminder", "Erinnerung"
        UNKNOWN = "unknown", "Unbekannt"

    class State(models.TextChoices):
        PENDING = "pending", "Offen"
        APPLIED = "applied", "Verarbeitet"
        NEEDS_REVIEW = "needs_review", "Klärungsbedarf"
        FAILED = "failed", "Fehler"
        REVIEWED = "reviewed", "Geklärt"
        DISMISSED = "dismissed", "Verworfen"

    class ReviewReason(models.TextChoices):
        UNKNOWN = "", "Grund nicht erfasst"
        UNRECOGNIZED_TYPE = "unrecognized_type", "E-Mail-Typ konnte nicht bestimmt werden"
        UNSUPPORTED_TYPE = "unsupported_type", "E-Mail-Typ wird manuell geprüft"
        REASSIGNMENT = "reassignment", "Ummeldung manuell zuordnen"
        SCHEDULE_CHANGE = "schedule_change", "Terminänderung manuell prüfen"
        INVALID_REFERENCE = "invalid_reference", "Buchungsreferenz ist ungültig oder mehrdeutig"
        PARTICIPANT_UNCLEAR = "participant_unclear", "Teilnehmerangabe fehlt oder ist mehrdeutig"
        COURSE_UNCLEAR = "course_unclear", "Lehrgangsangabe fehlt oder ist mehrdeutig"
        SCHOOL_UNCLEAR = "school_unclear", "Schulungsort fehlt oder ist mehrdeutig"
        PERIOD_UNCLEAR = "period_unclear", "Lehrgangszeitraum fehlt oder ist ungültig"
        REFERENCE_MISMATCH = "reference_mismatch", "Buchungsreferenz widerspricht der Zuordnung"
        MULTIPLE_SESSIONS = "multiple_sessions", "Mehrere passende Lehrgangstermine"
        MULTIPLE_SEATS = "multiple_seats", "Mehrere passende Lehrgangsplätze"
        MULTIPLE_COURSES = "multiple_courses", "Mehrere passende Lehrgänge"
        PREVIOUS_PARTICIPANT = "previous_participant", "Teilnehmer steht in der Buchungshistorie"
        NO_MATCHING_BOOKING = "no_matching_booking", "Kein passender Lehrgangsplatz vorhanden"
        EVENT_ORDER_UNCLEAR = "event_order_unclear", "Reihenfolge der Statusmeldungen ist unklar"
        PROTECTED_STATUS = "protected_status", "Vorhandener Buchungsstatus erfordert manuelle Prüfung"

    class ReviewResolution(models.TextChoices):
        LINKED = "linked", "Einem Lehrgangsplatz zugeordnet"
        REASSIGNED = "reassigned", "Ummeldung in BMS übernommen"
        DUPLICATE = "duplicate", "Doppelter Vorgang"
        INCOMPLETE = "incomplete", "Angaben reichen für die Zuordnung nicht aus"
        NOT_RELEVANT = "not_relevant", "Für BMS nicht relevant"

    sha256 = models.CharField(
        max_length=64,
        unique=True,
        validators=[RegexValidator(r"\A[0-9a-f]{64}\Z", "Ein SHA-256-Fingerabdruck wird benötigt.")],
    )
    kind = models.CharField(max_length=24, choices=Kind.choices, default=Kind.UNKNOWN)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    seat = models.ForeignKey(
        CourseSeat,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="email_events",
    )
    occurred_at = models.DateTimeField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    review_reason = models.CharField(max_length=24, choices=ReviewReason.choices, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_resolution = models.CharField(max_length=16, choices=ReviewResolution.choices, blank=True)


class BookingHistory(models.Model):
    seat = models.ForeignKey(CourseSeat, on_delete=models.CASCADE, related_name="history")
    previous_participant = models.ForeignKey(
        Participant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="previous_bookings",
    )
    participant = models.ForeignKey(
        Participant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="booking_history",
    )
    previous_status = models.CharField(max_length=24, choices=BookingStatus.choices, blank=True)
    status = models.CharField(max_length=24, choices=BookingStatus.choices)
    source_event = models.ForeignKey(
        EmailEvent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="booking_changes",
    )
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["recorded_at", "pk"]


class Carpool(models.Model):
    class Direction(models.TextChoices):
        OUTBOUND = "outbound", "Hinfahrt"
        RETURN = "return", "Rückfahrt"

    school = models.CharField(max_length=16, choices=School.choices)
    direction = models.CharField(max_length=8, choices=Direction.choices)
    travel_at = models.DateTimeField(null=True, blank=True)


class CarpoolMember(models.Model):
    carpool = models.ForeignKey(Carpool, on_delete=models.CASCADE, related_name="members")
    participant = models.ForeignKey(
        Participant,
        on_delete=models.CASCADE,
        related_name="carpool_memberships",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["carpool", "participant"],
                name="bookings_carpool_unique_member",
            ),
        ]


class Consent(models.Model):
    class Purpose(models.TextChoices):
        HOME_BRIGADE_RETENTION = "home_brigade_retention", "Heimatfeuerwehr speichern"
        CONTACT_SHARING = "contact_sharing", "Kontaktdaten zur Fahrgemeinschaft freigeben"

    purpose = models.CharField(max_length=24, choices=Purpose.choices)
    participant = models.ForeignKey(
        Participant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="consents",
    )
    member = models.ForeignKey(
        CarpoolMember,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="consents",
    )
    notice_version = models.CharField(max_length=64)
    granted_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    purpose="home_brigade_retention",
                    participant__isnull=False,
                    member__isnull=True,
                )
                | Q(
                    purpose="contact_sharing",
                    participant__isnull=True,
                    member__isnull=False,
                ),
                name="bookings_consent_matches_scope",
            ),
            models.CheckConstraint(
                condition=Q(revoked_at__isnull=True) | Q(revoked_at__gte=F("granted_at")),
                name="bookings_consent_revoked_after_grant",
            ),
            models.UniqueConstraint(
                fields=["participant"],
                condition=Q(purpose="home_brigade_retention", revoked_at__isnull=True),
                name="bookings_consent_active_home_brigade",
            ),
            models.UniqueConstraint(
                fields=["member"],
                condition=Q(purpose="contact_sharing", revoked_at__isnull=True),
                name="bookings_consent_active_carpool",
            ),
        ]


class ImportLock(models.Model):
    """A database row that serializes matching when no candidate row exists yet."""

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(pk=1), name="bookings_single_import_lock"),
        ]
