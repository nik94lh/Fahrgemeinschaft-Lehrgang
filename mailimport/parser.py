"""Pure local extraction of provisional fields from an EML message."""

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email import policy
from email.errors import MessageError
from email.headerregistry import Address
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from .email_address import normalize_contact_email
from .html_text import html_to_text


MAX_EML_BYTES = 1024 * 1024
BERLIN = ZoneInfo("Europe/Berlin")

SCHOOLS = {
    "wuerzburg": r"\b(?:würzburg|wuerzburg)\b",
    "regensburg": r"\bregensburg\b",
    "geretsried": r"\bgeretsried\b",
}
MONTHS = {
    month: number
    for number, month in enumerate(
        ("januar", "februar", "märz", "april", "mai", "juni", "juli", "august",
         "september", "oktober", "november", "dezember"),
        start=1,
    )
}
MONTHS["maerz"] = 3
CONFIRMATION_SUBJECT = r"^(?:teilnahmebestätigung|lehrgangsanmeldung wurde bestätigt)\b"
BOOKING_SUBJECT = "ihre lehrgangsanmeldung"

SUBJECT_PATTERNS = (
    ("automatic_cancellation", r"^automatische stornierung\b"),
    ("cancellation", r"^stornierung\b|^lehrgangsanmeldung wurde storniert\b"),
    ("reassignment", r"^ummeldung\b|^lehrgangsanmeldung wurde umgemeldet\b"),
    ("schedule_change", r"^terminänderung\b"),
    ("reminder", r"^erinnerung\b"),
    ("confirmation_request", r"^bestätigungsaufforderung\b|^bitte bestätigen sie\b"),
    ("confirmation", CONFIRMATION_SUBJECT),
    ("approval", r"^lehrgangsgenehmigung\b"),
    ("invitation", r"^einladung\b"),
)
BODY_PATTERNS = (
    ("approval", r"^ihre teilnahme an folgendem lehrgangstermin wurde genehmigt:"),
    ("confirmation", r"^die lehrgangsanmeldung (?:des teilnehmers|der teilnehmerin)"
                     r" .+? wurde bestätigt:"),
)
PAST_CANCELLATION = re.compile(
    r"^(?:ihre|die) (?:lehrgangs(?:anmeldung|teilnahme)|anmeldung|teilnahme)\b"
    r"[^.!?\n]{0,140}?(?:\bwurde\b[^.!?\n]{0,55}\bstorniert\b"
    r"|\bstorniert (?:wurde|worden)\b)",
    re.IGNORECASE | re.MULTILINE,
)
CANCELLATION_TYPES = {"cancellation", "automatic_cancellation"}
CANCELLATION_NEGATION = re.compile(r"\b(?:nicht|nie|niemals|keineswegs|weder)\b", re.IGNORECASE)
FORWARD_PREFIX = re.compile(r"^\s*(?:WG|FW|FWD):\s*", re.IGNORECASE)
REPLY_PREFIX = re.compile(r"^\s*(?:AW|RE):\s*", re.IGNORECASE)
FORWARD_HEADER = re.compile(
    r"^(?:von|from|an|to|cc|datum|date|gesendet|sent|betreff|subject) *:",
    re.IGNORECASE,
)
FORWARD_SEPARATOR = re.compile(
    r"^\s*-{2,}\s*(?:weitergeleitete nachricht|ursprüngliche nachricht|forwarded message|original message)"
    r"\s*-{2,}\s*$", re.IGNORECASE | re.MULTILINE,
)
OUTLOOK_FORWARD_HEADERS = re.compile(
    r"(?:\A|\n\n)(?=[ \t]*(?:von|from) *: *[^\n]+\n"
    r"[ \t]*(?:gesendet|sent) *: *[^\n]+\n"
    r"[ \t]*(?:an|to) *: *[^\n]*\n"
    r"(?:[ \t]*cc *: *[^\n]*\n)?"
    r"[ \t]*(?:betreff|subject) *: *[^\n]+\n)",
    re.IGNORECASE,
)
TIME_RANGE = re.compile(
    r"(\d{1,2}\.\d{1,2}\.\d{4} \d{1,2}:\d{2})(?: Uhr)?"
    r"\s*(?:[–—-]|bis)\s*"
    r"(\d{1,2}\.\d{1,2}\.\d{4} \d{1,2}:\d{2})(?: Uhr)?",
    re.IGNORECASE,
)
DATE_PATTERN = r"(\d{1,2})\.\s*([a-zäöü]+|\d{1,2}\.)\s*(\d{4})\b"
NAME_PATTERN = r"[^\W\d_]+(?:[.’' -][^\W\d_]+)+\.?"
GREETING = r"^sehr geehrte(?:r)?\s+(?:herr|frau)\s+([^,\n]+),"
CONFIRMED_PARTICIPANT = (
    r"^die lehrgangsanmeldung (?:des teilnehmers|der teilnehmerin) ([^\n]+?) für den folgenden\b"
)


class MailParseError(ValueError):
    """The message cannot be inspected unambiguously."""


@dataclass(frozen=True, slots=True)
class ParsedMail:
    fingerprint: str
    event_type: str
    course_name: str | None
    participant_name: str | None
    previous_participant_name: str | None
    school: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    confirmation_deadline: date | None
    cancellation_date: date | None
    booking_reference: str | None
    warnings: tuple[str, ...]
    reference_conflict: bool = False
    participant_email: str | None = field(default=None, repr=False)
    participant_email_ambiguous: bool = False
    address_note: str = ""
    sent_at: datetime | None = None


def _normalized(value: str) -> str:
    return " ".join(value.split())


def _unique(values: list[str], validate: Callable[[str], bool]) -> str | None:
    candidates = {_normalized(value) for value in values}
    if len(candidates) != 1:
        return None
    value = candidates.pop()
    return value if validate(value) else None


def _values(text: str, labels: str) -> list[str]:
    return re.findall(rf"^(?:{labels}) *: *([^\n]+)$", text, re.IGNORECASE | re.MULTILINE)


def _name(value: str) -> bool:
    return len(value) <= 150 and bool(re.fullmatch(NAME_PATTERN, value))


def _course_name(value: str) -> bool:
    return (
        len(value) <= 160
        and not re.search(r"\b[\w-]+\.[a-zäöü]{2,63}\b|//", value, re.IGNORECASE)
        and bool(re.fullmatch(r"[^\W_][\w .,&()'’–/-]*", value))
    )


def _booking_reference(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)*", value)) and len(value) <= 64


def _header_addresses(values: list[str]) -> tuple[Address, ...]:
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


def _inline_addresses(envelope: str, labels: str, *, outlook_recipients: bool = False) -> tuple[Address, ...]:
    values = re.findall(rf"^(?:{labels}) *: *([^\n]*)$", envelope, re.IGNORECASE | re.MULTILINE)
    addresses = _header_addresses(values)
    if addresses or not outlook_recipients or len(values) != 1 or ";" not in values[0]:
        return addresses

    # Outlook quotes original recipients with semicolons. Accept only complete addresses.
    parts = tuple(_header_addresses([value.strip()]) for value in values[0].split(";"))
    if all(len(part) == 1 and normalize_contact_email(part[0].addr_spec) for part in parts):
        return tuple(part[0] for part in parts)
    return ()


def _contact_subject(subject: str) -> bool:
    return subject.casefold() == BOOKING_SUBJECT or bool(re.match(CONFIRMATION_SUBJECT, subject, re.IGNORECASE))


def _participant_email(
    recipients: tuple[Address, ...], participant_name: str, functional_addresses: Iterable[str],
    *, allow_unlabeled: bool,
) -> tuple[str | None, bool]:
    excluded = {email for address in functional_addresses if (email := normalize_contact_email(address))}
    remaining = []
    for recipient in recipients:
        email = normalize_contact_email(recipient.addr_spec)
        if email is None:
            return None, False
        if email not in excluded:
            remaining.append((_normalized(recipient.display_name).casefold(), email))

    target = participant_name.casefold()
    named_matches = {email for name, email in remaining if name == target}
    if len(named_matches) == 1:
        match = named_matches.pop()
        if any(email == match and name and name != target for name, email in remaining):
            return None, True
        return match, False
    if len(named_matches) > 1:
        return None, True
    if allow_unlabeled:
        if len({email for _, email in remaining}) > 1:
            return None, True
        if excluded and len(remaining) == 1 and not remaining[0][0]:
            return remaining[0][1], False
    return None, False


def _body_text(part: EmailMessage) -> str:
    try:
        value = part.get_content()
    except LookupError:
        value = part.get_payload(decode=True).decode("utf-8", errors="replace")
    return html_to_text(value) if part.get_content_subtype() == "html" else value


def _attached_messages(message: EmailMessage) -> list[EmailMessage]:
    child_messages = []
    for part in message.iter_parts():
        if part.get_content_type() == "message/rfc822":
            child_messages.extend(part.get_payload())
        elif part.get_content_maintype() == "multipart":
            child_messages.extend(_attached_messages(part))
    return child_messages


def _inline_forward(text: str, *, forwarded: bool) -> tuple[str, str, tuple[Address, ...]] | None:
    lines = [re.sub(r"^\s*(?:>\s*)+", "", line) for line in text.splitlines()]
    unfolded = "\n".join(lines)
    separators = list(FORWARD_SEPARATOR.finditer(unfolded))
    if not separators and forwarded:
        separators = list(OUTLOOK_FORWARD_HEADERS.finditer(unfolded))
    if not separators:
        return None
    if len(separators) > 1:
        raise MailParseError("Die Weiterleitung enthält mehrere zitierte Nachrichten.")

    lines = unfolded[separators[0].end():].strip().splitlines()
    headers = []
    first_gap = None
    for index, line in enumerate(lines):
        line = _normalized(line)
        if line and not FORWARD_HEADER.match(line):
            envelope = "\n".join(headers)
            subjects = _values(envelope, "Betreff|Subject")
            if len(subjects) == 1:
                recipients = ()
                if _contact_subject(subjects[0]):
                    original_headers = "\n".join(headers[:first_gap])
                    senders = _inline_addresses(original_headers, "Von|From")
                    original_subject = _values(original_headers, "Betreff|Subject")
                    if (
                        original_subject == subjects
                        and len(senders) == 1
                        and normalize_contact_email(senders[0].addr_spec)
                    ):
                        recipients = _inline_addresses(original_headers, "An|To", outlook_recipients=True)
                return subjects[0], "\n".join(lines[index:]), recipients
            break
        if line:
            headers.append(line)
        elif first_gap is None:
            first_gap = len(headers)
    raise MailParseError("Die zitierte Weiterleitung ist unvollständig.")


def _original_text(message: EmailMessage) -> tuple[str, str, tuple[Address, ...]]:
    subject = _normalized(str(message.get("Subject", "")))
    forwarded = bool(FORWARD_PREFIX.match(subject))
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None or forwarded:
        attached = _attached_messages(message)
        if len(attached) > 1:
            raise MailParseError("Die Weiterleitung enthält mehrere E-Mails.")
        if attached:
            return _original_text(attached[0])

    if body is None:
        raise MailParseError("Die E-Mail enthält keinen lesbaren Klartext- oder HTML-Teil.")

    text = _body_text(body)
    recipients = ()
    if not REPLY_PREFIX.match(subject):
        original = _inline_forward(text, forwarded=forwarded)
        if original:
            subject, text, inline_recipients = original
            if forwarded:
                recipients = inline_recipients
        elif _contact_subject(subject) and len(message.get_all("Subject")) == 1:
            recipients = _header_addresses([
                value for name, value in message.raw_items() if name.casefold() == "to"
            ])
    subject = FORWARD_PREFIX.sub("", subject, count=1)
    cleaned = "\n".join(_normalized(line) for line in text.splitlines())
    if not cleaned.strip():
        raise MailParseError("Die E-Mail enthält keinen lesbaren Inhalt.")
    return subject, cleaned, recipients


def _classify(subject: str, text: str) -> str:
    matches = {
        event_type for event_type, pattern in BODY_PATTERNS
        if re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    }
    matches.update(
        event_type for event_type, pattern in SUBJECT_PATTERNS
        if re.search(pattern, subject, re.IGNORECASE)
    )
    cancellation_negated = False
    for cancellation in PAST_CANCELLATION.findall(text):
        if CANCELLATION_NEGATION.search(cancellation):
            cancellation_negated = True
            continue
        automatic = re.search(r"\bautomatisch\b", cancellation, re.IGNORECASE)
        matches.add("automatic_cancellation" if automatic else "cancellation")
    if cancellation_negated and matches & CANCELLATION_TYPES:
        return "unknown"
    if matches == CANCELLATION_TYPES:
        return "automatic_cancellation"
    return matches.pop() if len(matches) == 1 else "unknown"


def _berlin_time(value: str) -> datetime:
    naive = datetime.strptime(value, "%d.%m.%Y %H:%M")
    localized = naive.replace(tzinfo=BERLIN)
    if localized.utcoffset() != naive.replace(tzinfo=BERLIN, fold=1).utcoffset():
        raise ValueError("Mehrdeutige oder nicht existierende Ortszeit")
    return localized


def _period(text: str) -> tuple[datetime | None, datetime | None]:
    value = _unique(_values(text, "Lehrgangstermin|Zeitraum"), lambda field: bool(field))
    match = TIME_RANGE.fullmatch(value or "")
    if not match:
        return None, None
    try:
        starts_at, ends_at = map(_berlin_time, match.groups())
    except ValueError:
        return None, None
    if ends_at.astimezone(UTC) <= starts_at.astimezone(UTC):
        return None, None
    return starts_at, ends_at


def _date_from_match(pattern: str, text: str) -> date | None:
    matches = set(re.findall(pattern, text, re.IGNORECASE))
    if len(matches) != 1:
        return None
    day, month, year = matches.pop()
    number = int(month[:-1]) if month.endswith(".") else MONTHS.get(month.casefold())
    if number is None:
        return None
    try:
        return date(int(year), number, int(day))
    except ValueError:
        return None


def _sent_at(message: EmailMessage) -> datetime | None:
    values = [value for name, value in message.raw_items() if name.casefold() == "date"]
    if len(values) != 1:
        return None
    try:
        value = parsedate_to_datetime(values[0])
        return value.astimezone(UTC) if value.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def parse_eml(raw: bytes, *, functional_addresses: Iterable[str] = ()) -> ParsedMail:
    """Parse bounded bytes locally. The result contains structured fields only."""
    if not raw:
        raise MailParseError("Die EML-Datei ist leer.")
    if len(raw) > MAX_EML_BYTES:
        raise MailParseError("Die EML-Datei ist größer als 1 MiB.")
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        if not message.keys() or message.defects:
            raise MailParseError("Die EML-Kopfzeilen sind beschädigt oder fehlen.")
        subject, text, recipients = _original_text(message)
    except (MessageError, RecursionError) as error:
        raise MailParseError("Die EML-Struktur kann nicht gelesen werden.") from error

    # Replies can contain HTML quotations and must not create new events or preview stale fields.
    is_reply = bool(REPLY_PREFIX.match(subject))
    if is_reply:
        subject, text = "", ""
    event_type = _classify(subject, text)
    participant = _values(text, "Teilnehmer(?:in)?|Neue[rs]? Teilnehmer(?:in)?")
    if not participant:
        pattern = CONFIRMED_PARTICIPANT if event_type == "confirmation" else GREETING
        participant = re.findall(pattern, text, re.IGNORECASE | re.MULTILINE)
    participant_name = _unique(participant, _name)
    participant_email, participant_email_ambiguous = None, False
    if recipients and participant_name and event_type != "unknown":
        participant_email, participant_email_ambiguous = _participant_email(
            recipients, participant_name, functional_addresses,
            allow_unlabeled=subject.casefold() == BOOKING_SUBJECT,
        )
    address_note = ""
    if is_reply:
        address_note = "Die Mail ist als Antwort gekennzeichnet. Leite die Original-Buchungsmail als EML oder mit ihren ursprünglichen Kopfzeilen weiter."
    elif event_type in ("approval", "confirmation") and not participant_email:
        if participant_email_ambiguous:
            address_note = "Im ursprünglichen An-Feld kommen mehrere Adressen für die Person in Frage."
        elif not participant_name:
            address_note = "Die Adresse konnte ohne eindeutig erkannten Teilnehmernamen nicht geprüft werden."
        elif not _contact_subject(subject):
            address_note = "Für diesen Betreff ist keine automatische Adressübernahme eingerichtet."
        elif not recipients:
            address_note = "Das ursprüngliche An-Feld fehlt oder konnte nicht eindeutig gelesen werden."
        else:
            address_note = "Im ursprünglichen An-Feld steht keine eindeutig passende persönliche Adresse. Prüfe Anzeigenamen und hinterlegte Funktionsadressen."
    previous_name = _unique(_values(text, "Bisherige[rs]? Teilnehmer(?:in)?"), _name)
    course_name = _unique(_values(text, "Lehrgangsart|Lehrgang"), _course_name)
    references = re.findall(
        r"^(?:Buchungsreferenz|Buchungsnummer) *: *([^\n]*)$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    reference = _unique(references, _booking_reference)
    reference_conflict = bool(references) and reference is None
    locations = " ".join(_values(text, "Schulungsort|Lehrgangsort|Schule"))
    schools = [code for code, pattern in SCHOOLS.items() if re.search(pattern, locations, re.IGNORECASE)]
    school = schools[0] if len(schools) == 1 else None
    starts_at, ends_at = _period(text)
    deadline = _date_from_match(r"spätestens am (?:[a-zäöü]+, )?" + DATE_PATTERN, text)
    cancellation = _date_from_match(
        r"andernfalls erfolgt am (?:[a-zäöü]+, )?" + DATE_PATTERN + r" die stornierung", text,
    )

    checks = (
        (event_type == "unknown", "E-Mail-Typ nicht erkannt."),
        (participant_name is None, "Teilnehmername fehlt oder ist nicht eindeutig."),
        (course_name is None, "Lehrgangsart fehlt oder ist nicht eindeutig."),
        (school is None, "Schulungsort fehlt oder ist nicht eindeutig."),
        (starts_at is None, "Lehrgangszeitraum fehlt oder ist ungültig."),
        (reference_conflict, "Die Buchungsreferenz ist ungültig oder nicht eindeutig."),
        (participant_email_ambiguous, "Die E-Mail-Adresse des Teilnehmers ist nicht eindeutig."),
    )
    return ParsedMail(
        fingerprint=hashlib.sha256(raw).hexdigest(),
        event_type=event_type,
        course_name=course_name,
        participant_name=participant_name,
        previous_participant_name=previous_name,
        school=school,
        starts_at=starts_at,
        ends_at=ends_at,
        confirmation_deadline=deadline,
        cancellation_date=cancellation,
        booking_reference=reference,
        warnings=tuple(warning for invalid, warning in checks if invalid),
        reference_conflict=reference_conflict,
        participant_email=participant_email,
        participant_email_ambiguous=participant_email_ambiguous,
        address_note=address_note,
        sent_at=_sent_at(message),
    )
