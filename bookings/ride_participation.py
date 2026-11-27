"""Test invitations, replies and contact sharing for a single ride proposal."""

import re
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .models import (
    DEFAULT_RIDE_LEAD_DAYS,
    BookingStatus,
    RideCase,
    RideConfiguration,
    RideNotice,
    RideParticipation,
)
from .ride_codes import code_digest, compact_code_v1, display_code, legacy_code, normalize_code
from .rides_matching import BERLIN
from .services import _lock_import


CONTACT_CHANNELS = (("email", "E-Mail"), ("whatsapp", "WhatsApp"), ("call", "Anruf"))
_CHANNEL_LABELS = dict(CONTACT_CHANNELS)
_PHONE_CHANNELS = {"whatsapp", "call"}
_TEST_PHONE = re.compile(r"000(?:[ -]?[0-9]){3,12}")


def portal_enabled():
    return settings.HTTPS and bool(settings.RIDE_PORTAL_HOST)


def _test_email(email):
    return email.rpartition("@")[2].casefold().endswith(".invalid")


def invite_code(participation):
    return display_code(participation)


def _matching_test_seat(seat, case):
    if not seat.is_test or seat.status != BookingStatus.CONFIRMED or seat.participant is None:
        return False
    session = seat.session
    return (
        _test_email(seat.participant.email)
        and not session.is_online
        and session.school == case.school
        and session.starts_at.astimezone(BERLIN).date() == case.travel_date
        and session.ends_at.astimezone(BERLIN).date() == case.return_date
    )


def _open_test_case(case):
    return (
        case.is_test and case.removed_at is None and case.direction == RideCase.ROUND_TRIP
        and case.status != RideCase.Status.CLOSED
    )


def with_case_membership(queryset):
    seat_links = RideCase.seats.through.objects.filter(
        ridecase_id=OuterRef("case_id"), courseseat_id=OuterRef("seat_id"),
    )
    return queryset.annotate(is_case_member=Exists(seat_links))


def _entries():
    return with_case_membership(RideParticipation.objects.select_related(
        "case", "participant", "seat__participant", "seat__session__course",
    ))


def _has_access(entry, now):
    return (
        entry.seat_id is not None
        and entry.expires_at > now
        and entry.seat.participant_id == entry.participant_id
        and entry.seat.session_id == entry.session_id
        # After its own course starts, an invitation can remain valid without a future match.
        and ((entry.case.currently_matches and entry.is_case_member) or entry.seat.session.starts_at <= now)
        and entry.email == entry.participant.email
        and _matching_test_seat(entry.seat, entry.case)
    )


def _check_access(entry):
    if entry and _open_test_case(entry.case) and _has_access(entry, timezone.now()):
        return entry
    return None


def participation_for_code(code):
    code = normalize_code(code)
    if not portal_enabled() or code is None:
        return None
    field = "short_token_digest" if len(code) == 16 else "token_digest"
    return _check_access(_entries().filter(**{field: code_digest(code)}).first())


def active_participation(participation_id, token_digest):
    if not portal_enabled():
        return None
    return _check_access(_entries().filter(pk=participation_id, token_digest=token_digest).first())


@transaction.atomic
def set_ride_automation(enabled):
    _lock_import()
    RideConfiguration.objects.update_or_create(pk=1, defaults={"automatic_inquiries": enabled})


def uninvited_participants():
    invitations = RideParticipation.objects.filter(
        case_id=OuterRef("ridecase_id"), participant_id=OuterRef("courseseat__participant_id"),
    )
    return Exists(RideCase.seats.through.objects.filter(
        ridecase_id=OuterRef("pk"), courseseat__participant_id__isnull=False,
    ).filter(~Exists(invitations)))


@transaction.atomic
def start_ride_inquiry(case_id, *, automatic=False, now=None):
    if not portal_enabled():
        raise ValueError("Der Zugang für den Teilnahme-Test ist noch nicht eingerichtet.")
    _lock_import()
    now = now or timezone.now()
    case = RideCase.objects.select_for_update().filter(pk=case_id).first()
    if case is None or not _open_test_case(case) or not case.currently_matches or case.earliest <= now:
        raise ValueError("Für diesen Vorschlag ist derzeit keine Testabfrage möglich.")
    config = RideConfiguration.objects.filter(pk=1).first()
    automation_on = bool(config and config.automatic_inquiries)
    if automation_on != automatic:
        raise ValueError("Die Einstellung für den Abfragestart wurde geändert.")
    if automatic:
        lead_days = config.lead_days if config else DEFAULT_RIDE_LEAD_DAYS
        if case.travel_date > timezone.localdate(now, timezone=BERLIN) + timedelta(days=lead_days):
            return 0
    seats = {}
    for seat in case.seats.select_related("participant", "session").order_by("pk"):
        if _matching_test_seat(seat, case):
            seats.setdefault(seat.participant_id, seat)
    if len(seats) < 2:
        raise ValueError("Die Testabfrage braucht mindestens zwei passende Testbuchungen.")

    invited = set(case.participation_requests.values_list("participant_id", flat=True))
    entries = []
    for participant_id, seat in seats.items():
        if participant_id in invited:
            continue
        entry = RideParticipation(
            case=case, seat=seat, session=seat.session, participant=seat.participant,
            email=seat.participant.email, expires_at=seat.session.ends_at + timedelta(days=1),
        )
        entry.token_digest = code_digest(legacy_code(entry.token_seed))
        entry.short_token_digest = code_digest(compact_code_v1(entry.token_seed))
        entries.append(entry)
    RideParticipation.objects.bulk_create(entries)
    RideNotice.objects.bulk_create([
        RideNotice(participation=entry, kind=RideNotice.Kind.INVITATION) for entry in entries
    ])
    return len(entries)


def run_automatic_ride_inquiries(*, now=None):
    if not portal_enabled():
        return 0
    lead_days = RideConfiguration.objects.filter(pk=1, automatic_inquiries=True).values_list(
        "lead_days", flat=True,
    ).first()
    if lead_days is None:
        return 0
    now = now or timezone.now()
    case_ids = RideCase.objects.filter(
        is_test=True, direction=RideCase.ROUND_TRIP, currently_matches=True, earliest__gt=now,
        travel_date__lte=timezone.localdate(now, timezone=BERLIN) + timedelta(days=lead_days),
        removed_at__isnull=True,
    ).exclude(status=RideCase.Status.CLOSED).filter(uninvited_participants()).values_list("pk", flat=True)
    started = 0
    for case_id in case_ids:
        try:
            started += start_ride_inquiry(case_id, automatic=True, now=now)
        except ValueError:
            # The case or the setting can change between selection and lock acquisition.
            continue
    return started


def _consenting_entries(case):
    if not _open_test_case(case):
        return {}
    now = timezone.now()
    return {
        entry.pk: entry
        for entry in _entries().filter(
            case=case, answer=RideParticipation.Answer.YES, consented_at__isnull=False,
        ).order_by("participant__name", "pk")
        if _has_access(entry, now)
    }


def expire_booking_participation(seat_id):
    """Retire access under the import lock when the local booking is corrected."""
    now = timezone.now()
    for entry in _entries().filter(seat_id=seat_id, expires_at__gt=now):
        before = _consenting_entries(entry.case)
        entry.expires_at = now
        entry.save(update_fields=["expires_at"])
        if entry.pk in before and len(before) >= 2:
            RideNotice.objects.bulk_create([
                RideNotice(participation_id=entry_id, kind=RideNotice.Kind.WITHDRAWAL)
                for entry_id in before if entry_id != entry.pk
            ])


def portal_overview(participation):
    now = timezone.now()
    entries = _entries().filter(case_id=participation.case_id, expires_at__gt=now).order_by("participant__name", "pk")
    current = {entry.pk: entry for entry in entries if _has_access(entry, now)}
    own = current.pop(participation.pk, None)
    if own is None or not _open_test_case(own.case):
        return 0, []
    available = [entry for entry in current.values() if entry.answer != RideParticipation.Answer.NO]
    if own.answer != RideParticipation.Answer.YES or not own.consented_at:
        return len(available), []
    contacts = [
        {
            "name": entry.participant.name,
            "brigade": entry.brigade,
            "email": entry.email if "email" in entry.channels else "",
            "phone": entry.phone if _PHONE_CHANNELS.intersection(entry.channels) else "",
            "channels": [_CHANNEL_LABELS[channel] for channel in entry.channels],
        }
        for entry in available if entry.answer == RideParticipation.Answer.YES and entry.consented_at
    ]
    return len(available), contacts


def _reply_data(answer, brigade, phone, channels, consent):
    if answer == RideParticipation.Answer.NO:
        return "", "", []
    if answer != RideParticipation.Answer.YES or not consent:
        raise ValueError("Bestätige deine Auswahl und die Kontaktfreigabe.")
    selected = set(channels)
    if not selected or selected - _CHANNEL_LABELS.keys():
        raise ValueError("Wähle mindestens einen gültigen Kontaktweg.")
    brigade, phone = brigade.strip(), phone.strip()
    if len(brigade) > 120:
        raise ValueError("Die Feuerwehr darf höchstens 120 Zeichen lang sein.")
    if _PHONE_CHANNELS.intersection(selected):
        if not _TEST_PHONE.fullmatch(phone):
            raise ValueError("Verwende eine Testnummer wie 000 000 001.")
    else:
        phone = ""
    return brigade, phone, [channel for channel, _ in CONTACT_CHANNELS if channel in selected]


@transaction.atomic
def record_ride_reply(participation_id, token_digest, *, answer, brigade="", phone="", channels=(), consent=False):
    _lock_import()
    entry = active_participation(participation_id, token_digest)
    if entry is None:
        raise ValueError("Dieser Testzugang ist nicht mehr gültig.")
    case = RideCase.objects.select_for_update().get(pk=entry.case_id)
    if not _open_test_case(case):
        raise ValueError("Dieser Testzugang ist nicht mehr gültig.")
    brigade, phone, channels = _reply_data(answer, brigade, phone, channels, consent)
    if (entry.answer, entry.brigade, entry.phone, entry.channels) == (answer, brigade, phone, channels):
        return entry

    before = _consenting_entries(case)
    entry.answer, entry.brigade, entry.phone, entry.channels = answer, brigade, phone, channels
    entry.answered_at = timezone.now()
    entry.consented_at = entry.answered_at if answer == RideParticipation.Answer.YES else None
    entry.save(update_fields=["answer", "brigade", "phone", "channels", "answered_at", "consented_at"])
    after = _consenting_entries(case)
    shared_before = set(before) if len(before) >= 2 else set()
    shared_after = set(after) if len(after) >= 2 else set()
    newly_sharing = shared_after - shared_before
    notices = [
        RideNotice(participation_id=entry_id, kind=RideNotice.Kind.CONTACTS)
        for entry_id in sorted(newly_sharing)
    ]
    if newly_sharing:
        notices.extend(
            RideNotice(participation_id=entry_id, kind=RideNotice.Kind.NEW_PEER)
            for entry_id in sorted(shared_before & shared_after)
        )
    if entry.pk in shared_before and answer == RideParticipation.Answer.NO:
        notices.extend(
            RideNotice(participation_id=entry_id, kind=RideNotice.Kind.WITHDRAWAL)
            for entry_id in sorted(shared_before - {entry.pk})
        )
    RideNotice.objects.bulk_create(notices)
    return entry


def notice_message(notice):
    return {
        RideNotice.Kind.INVITATION: "Für deinen Lehrgang kommt eine Fahrgemeinschaft infrage.",
        RideNotice.Kind.CONTACTS: "Mindestens zwei Personen haben zugesagt. Die Kontaktdaten sind im Portal verfügbar.",
        RideNotice.Kind.NEW_PEER: "Eine weitere Person hat zugesagt und ihre Kontaktdaten freigegeben.",
        RideNotice.Kind.WITHDRAWAL: "Eine Zusage wurde nachträglich zurückgezogen. Die Kontaktliste wurde aktualisiert.",
    }[notice.kind]


@sensitive_variables()
def notice_preview(notice):
    participation = notice.participation
    case = participation.case
    school = case.get_school_display()
    start = case.travel_date.strftime("%d.%m.%Y")
    period = start if case.return_date == case.travel_date else f"{start} bis {case.return_date:%d.%m.%Y}"
    course = participation.session.course.name if participation.session else "Lehrgang"
    label, action = {
        RideNotice.Kind.INVITATION: (
            "Mögliche Fahrgemeinschaft",
            "Gib bitte an, ob du Interesse an der Fahrgemeinschaft hast. "
            "Bei einer Zusage wählst du die Kontaktwege, die du freigeben willst.",
        ),
        RideNotice.Kind.CONTACTS: (
            "Kontakte freigegeben",
            "Melde dich im Teilnahmeportal an, um die freigegebenen Kontaktdaten zu sehen und euch abzusprechen.",
        ),
        RideNotice.Kind.NEW_PEER: (
            "Weitere Zusage",
            "Die aktualisierte Kontaktliste findest du im Teilnahmeportal.",
        ),
        RideNotice.Kind.WITHDRAWAL: (
            "Rückmeldung zur Fahrgemeinschaft geändert",
            "Den aktuellen Stand findest du im Teilnahmeportal.",
        ),
    }[notice.kind]
    code = invite_code(participation)
    portal = f"https://{settings.RIDE_PORTAL_HOST}{reverse('ride_portal')}"
    link = f"{portal}#{code}"
    return {
        "recipient": participation.email,
        "subject": f"Testvorschau · {label} · {school} · {period}",
        "body": (
            f"Hallo {participation.participant.name},\n\n"
            f"{course} · {school} · {period}\n\n"
            f"{notice_message(notice)} {action}\n\n"
            f"Direkt zum Teilnahmeportal: {link}\n"
            f"Alternativ öffne {portal} und gib deinen persönlichen Zugangscode ein: {code}"
        ),
        "link": link,
        "code": code,
    }
