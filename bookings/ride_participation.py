"""Test invitations, replies and contact sharing for a single ride proposal."""

import re
from base64 import urlsafe_b64encode
from datetime import timedelta
from hashlib import sha256

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import (
    DEFAULT_RIDE_LEAD_DAYS,
    BookingStatus,
    RideCase,
    RideConfiguration,
    RideNotice,
    RideParticipation,
)
from .rides_matching import BERLIN
from .services import _lock_import


CONTACT_CHANNELS = (("email", "E-Mail"), ("whatsapp", "WhatsApp"), ("call", "Anruf"))
_CHANNEL_LABELS = dict(CONTACT_CHANNELS)
_PHONE_CHANNELS = {"whatsapp", "call"}
_TEST_PHONE = re.compile(r"000(?:[ -]?[0-9]){3,12}")
_ACCESS_CODE = re.compile(r"[A-Za-z0-9_-]{43}")


def portal_enabled():
    return settings.HTTPS and bool(settings.RIDE_PORTAL_HOST)


def _test_email(email):
    return email.rpartition("@")[2].casefold().endswith(".invalid")


def invite_code(participation):
    # An operator can reopen the preview without storing a usable code in the database.
    key = salted_hmac(
        "bookings.ride_participation.code", str(participation.token_seed), algorithm="sha256",
    ).digest()
    return urlsafe_b64encode(key).decode("ascii").rstrip("=")


def _digest(code):
    return sha256(code.encode("ascii")).hexdigest()


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
    return case.is_test and case.direction == RideCase.ROUND_TRIP and case.status != RideCase.Status.CLOSED


def _entries():
    return RideParticipation.objects.select_related("case", "seat", "participant", "seat__session__course")


def _has_access(entry, now):
    # The saved invitation stays tied to its booking when the matcher drops an already started course.
    return (
        entry.seat_id is not None
        and entry.expires_at > now
        and entry.seat.participant_id == entry.participant_id
        and entry.seat.session_id == entry.session_id
        and entry.email == entry.participant.email
        and _matching_test_seat(entry.seat, entry.case)
    )


def _check_access(entry):
    if entry and _open_test_case(entry.case) and _has_access(entry, timezone.now()):
        return entry
    return None


def participation_for_code(code):
    if not portal_enabled() or not _ACCESS_CODE.fullmatch(code):
        return None
    return _check_access(_entries().filter(token_digest=_digest(code)).first())


def active_participation(participation_id, token_digest):
    if not portal_enabled():
        return None
    return _check_access(_entries().filter(pk=participation_id, token_digest=token_digest).first())


@transaction.atomic
def set_ride_automation(enabled):
    _lock_import()
    RideConfiguration.objects.update_or_create(pk=1, defaults={"automatic_inquiries": enabled})


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
    if case.participation_requests.exists():
        return 0

    seats = {}
    for seat in case.seats.select_related("participant", "session").order_by("pk"):
        if _matching_test_seat(seat, case):
            seats.setdefault(seat.participant_id, seat)
    if len(seats) < 2:
        raise ValueError("Die Testabfrage braucht mindestens zwei passende Testbuchungen.")

    entries = []
    for seat in seats.values():
        entry = RideParticipation(
            case=case, seat=seat, session=seat.session, participant=seat.participant,
            email=seat.participant.email, expires_at=seat.session.ends_at + timedelta(days=1),
        )
        entry.token_digest = _digest(invite_code(entry))
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
        participation_requests__isnull=True,
    ).exclude(status=RideCase.Status.CLOSED).values_list("pk", flat=True)
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


def shared_contacts(participation):
    entries = _consenting_entries(participation.case)
    if len(entries) < 2 or participation.pk not in entries:
        return []
    return [
        {
            "name": entry.participant.name,
            "brigade": entry.brigade,
            "email": entry.email if "email" in entry.channels else "",
            "phone": entry.phone if _PHONE_CHANNELS.intersection(entry.channels) else "",
            "channels": [_CHANNEL_LABELS[channel] for channel in entry.channels],
        }
        for entry in entries.values() if entry.pk != participation.pk
    ]


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
    notices = [
        RideNotice(participation_id=entry_id, kind=RideNotice.Kind.CONTACTS)
        for entry_id in sorted(shared_after - shared_before)
    ]
    if entry.pk in shared_before and answer == RideParticipation.Answer.NO:
        notices.extend(
            RideNotice(participation_id=entry_id, kind=RideNotice.Kind.WITHDRAWAL)
            for entry_id in sorted(shared_before - {entry.pk})
        )
    RideNotice.objects.bulk_create(notices)
    return entry


def notice_message(notice):
    return {
        RideNotice.Kind.INVITATION: "Du bist zur Testabfrage für die Fahrgemeinschaft eingeladen.",
        RideNotice.Kind.CONTACTS: "Mindestens zwei Personen haben zugesagt. Die Kontaktdaten sind im Portal verfügbar.",
        RideNotice.Kind.WITHDRAWAL: "Eine Zusage wurde nachträglich zurückgezogen. Die Kontaktliste wurde aktualisiert.",
    }[notice.kind]


def notice_preview(notice):
    code = invite_code(notice.participation)
    portal = f"https://{settings.RIDE_PORTAL_HOST}{reverse('ride_portal')}"
    link = f"{portal}#{code}"
    return {
        "recipient": notice.participation.email,
        "subject": f"Testvorschau · {notice.get_kind_display()}",
        "body": f"{notice_message(notice)}\n\nSeite: {portal}\nCode: {code}\nDirektlink: {link}",
        "link": link,
        "code": code,
    }
