"""Decode original recipient headers and evaluate evidence for a participant address."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from email import policy
from email.headerregistry import Address
from unicodedata import normalize

from .email_address import normalize_contact_email


REPEATED_MAILTO = re.compile(r"<([^<>\s]+)<mailto:([^<>\s]+)>>", re.IGNORECASE)
GERMAN_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue"})


def header_addresses(values: list[str]) -> tuple[Address, ...]:
    if len(values) != 1:
        return ()
    try:
        # BytesParser uses surrogateescape for raw UTF-8 header bytes (SMTPUTF8).
        value = values[0].encode("utf-8", errors="surrogateescape").decode("utf-8")
        header = policy.default.header_fetch_parse("To", value)
    except ValueError:
        return ()
    if header.defects or any(group.display_name is not None for group in header.groups):
        return ()
    return header.addresses


def inline_addresses(envelope: str, labels: str, *, outlook_recipients: bool = False) -> tuple[Address, ...]:
    values = re.findall(rf"^(?:{labels}) *: *([^\n]*)$", envelope, re.IGNORECASE | re.MULTILINE)
    values = [REPEATED_MAILTO.sub(_collapse_repeated_mailto, value) for value in values]
    addresses = header_addresses(values)
    if addresses or not outlook_recipients or len(values) != 1 or ";" not in values[0]:
        return addresses

    # Outlook quotes original recipients with semicolons. Accept only complete addresses.
    parts = tuple(header_addresses([value.strip()]) for value in values[0].split(";"))
    if all(len(part) == 1 and normalize_contact_email(part[0].addr_spec) for part in parts):
        return tuple(part[0] for part in parts)
    return ()


def _collapse_repeated_mailto(match: re.Match) -> str:
    visible, repeated = match.groups()
    if visible.casefold() == repeated.casefold():
        return f"<{visible}>"
    return match[0]


@dataclass(frozen=True, slots=True)
class AddressSelection:
    email: str | None = None
    ambiguous: bool = False
    note: str = ""


def select_participant_address(
    recipients: tuple[Address, ...], participant_name: str, functional_addresses: Iterable[str],
    *, allow_local_part_match: bool, allow_approval_fallback: bool,
) -> AddressSelection:
    excluded = {value for entry in functional_addresses if (value := normalize_contact_email(entry))}
    remaining = []
    excluded_from_original = False
    for recipient in recipients:
        email = normalize_contact_email(recipient.addr_spec)
        if email is None:
            return AddressSelection(note="Das ursprüngliche An-Feld enthält eine ungültige Adresse.")
        if email in excluded:
            excluded_from_original = True
        else:
            display = normalize("NFC", " ".join(recipient.display_name.split())).casefold()
            remaining.append((display, email))

    if not remaining:
        return AddressSelection(note="Alle Adressen im ursprünglichen An-Feld sind als Funktionsadressen ausgeschlossen.")

    target = participant_name.casefold()
    named_matches = {email for display, email in remaining if display == target}
    if len(named_matches) == 1:
        email = next(iter(named_matches))
        if not any(address == email and display and display != target for display, address in remaining):
            return AddressSelection(email=email)
    if named_matches or (allow_approval_fallback and len({email for _, email in remaining}) > 1):
        return AddressSelection(ambiguous=True, note="Im ursprünglichen An-Feld kommen mehrere Zuordnungen in Frage.")

    if len(remaining) == 1:
        display, email = remaining[0]
        if display in ("", email):
            name_parts = target.translate(GERMAN_UMLAUTS).split()
            full_name_matches = len(name_parts) >= 2 and email.rsplit("@", 1)[0] == ".".join(name_parts)
            if (allow_local_part_match and full_name_matches) or (
                allow_approval_fallback and not display and excluded_from_original
            ):
                return AddressSelection(email=email)
            return AddressSelection(note="Das Original nennt eine Adresse ohne eindeutigen Bezug zum vollständigen Teilnehmernamen.")

    return AddressSelection(note="Der Empfängername im ursprünglichen An-Feld passt nicht eindeutig zum Teilnehmer.")
