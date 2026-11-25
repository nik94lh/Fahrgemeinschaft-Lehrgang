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
from unicodedata import normalize
from zoneinfo import ZoneInfo

from .email_address import normalize_contact_email
from .html_text import html_to_text
from .recipient_address import AddressSelection, header_addresses, inline_addresses, select_participant_address


MAX_EML_BYTES = 1024 * 1024
MAX_INLINE_FORWARD_LAYERS = 12
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
PERSONAL_CONFIRMATION_SUBJECT = r"^(?:ihr lehrgangstermin wurde bestätigt|bestätigung ihrer lehrgangsanmeldung)[.!]?$"
CONFIRMATION_SUBJECT = (
    r"^(?:teilnahmebestätigung|lehrgangsanmeldung wurde bestätigt)\b"
    rf"|{PERSONAL_CONFIRMATION_SUBJECT}"
)
BOOKING_SUBJECT = "ihre lehrgangsanmeldung"
APPROVAL_SUBJECT = r"^lehrgangsgenehmigung\b"

SUBJECT_PATTERNS = (
    ("automatic_cancellation", r"^automatische stornierung\b"),
    ("cancellation", r"^stornierung\b|^lehrgangsanmeldung wurde storniert\b"),
    ("reassignment", r"^ummeldung\b|^lehrgangsanmeldung wurde umgemeldet\b"),
    ("schedule_change", r"^terminänderung\b"),
    ("reminder", r"^erinnerung\b"),
    ("confirmation_request", r"^bestätigungsaufforderung\b|^bitte bestätigen sie\b"),
    ("confirmation", CONFIRMATION_SUBJECT),
    ("approval", APPROVAL_SUBJECT),
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
    r"^(von|from|an|to|cc|datum|date|gesendet|sent|betreff|subject) *:",
    re.IGNORECASE,
)
FORWARD_DATE_LINE = r"[ \t]*(?:gesendet|sent|datum|date) *: *[^\n]+\n"
FORWARD_TO_LINE = r"[ \t]*(?:an|to) *: *[^\n]*\n"
INLINE_FORWARD_START = re.compile(
    r"^[ \t]*-{2,}[ \t]*(?:weitergeleitete nachricht|ursprüngliche nachricht|forwarded message|original message)"
    r"[ \t]*-{2,}[ \t]*$"
    r"|(?=^[ \t]*(?:von|from) *: *[^\n]+\n"
    rf"(?:{FORWARD_DATE_LINE}{FORWARD_TO_LINE}|{FORWARD_TO_LINE}{FORWARD_DATE_LINE})"
    r"(?:[ \t]*cc *: *[^\n]*\n)?"
    r"[ \t]*(?:betreff|subject) *: *[^\n]+\n)",
    re.IGNORECASE | re.MULTILINE,
)
LOCATION_LABELS = "Schulungsort|Lehrgangsort|Veranstaltungsort|Schule"
ONLINE_FORMAT = re.compile(
    r"^(?:online(?:[-–— ]?(?:lehrgang|seminar|veranstaltung|unterricht|schulung|kurs))?"
    r"|virtuell|webinar|e[- ]?learning|digital)(?=$|[ (,:])", re.IGNORECASE,
)
ONLINE_COURSE = re.compile(
    r"^(?:online[-–— ]?(?:lehrgang|seminar|veranstaltung|unterricht|schulung|kurs)|webinar|e[- ]?learning)\b",
    re.IGNORECASE,
)
ONLINE_SUFFIX = re.compile(r"\(\s*(?:online|webinar|e[- ]?learning)\s*\)$", re.IGNORECASE)
IN_PERSON_FORMAT = re.compile(
    r"\b(?:hybrid\w*|blended learning|präsenz\w*|vor ort)\b", re.IGNORECASE,
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
    is_online: bool = False
    unusable_fields: tuple[str, ...] = ()
    participant_email_candidate: str | None = field(default=None, repr=False)


def _normalized(value: str) -> str:
    return normalize("NFC", " ".join(value.split()))


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


def _contact_subject(subject: str) -> bool:
    return subject.casefold() == BOOKING_SUBJECT or any(
        re.match(pattern, subject, re.IGNORECASE) for pattern in (APPROVAL_SUBJECT, CONFIRMATION_SUBJECT)
    )


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


@dataclass(frozen=True, slots=True)
class _InlineMail:
    subject: str
    text: str
    recipients: tuple[Address, ...]
    sender: str | None
    name_only_recipient: str | None


@dataclass(frozen=True, slots=True)
class _OriginalMail:
    subject: str
    text: str
    recipients: tuple[Address, ...] = ()
    source: str = "Ursprüngliches An-Feld"
    conflict: bool = False


def _inline_forward(text: str) -> _InlineMail | None:
    unfolded = "\n".join(re.sub(r"^\s*(?:>\s*)+", "", line) for line in text.splitlines())
    start = INLINE_FORWARD_START.search(unfolded)
    if start is None:
        return None

    for _ in range(MAX_INLINE_FORWARD_LAYERS):
        quoted = _inline_layer(unfolded[start.end():])
        if REPLY_PREFIX.match(quoted.subject):
            return quoted
        unfolded = quoted.text
        start = INLINE_FORWARD_START.search(unfolded)
        if start is None:
            return quoted
        if not FORWARD_PREFIX.match(quoted.subject):
            raise MailParseError("Die Weiterleitung enthält mehrere zitierte Nachrichten.")
    raise MailParseError("Die Weiterleitung enthält zu viele verschachtelte Nachrichten.")


def _inline_layer(text: str) -> _InlineMail:
    lines = text.strip().splitlines()
    headers = []
    first_gap = None
    has_subject = False
    for index, raw_line in enumerate(lines):
        line = _normalized(raw_line)
        if header := FORWARD_HEADER.match(line):
            headers.append(line)
            has_subject = has_subject or header[1].casefold() in ("betreff", "subject")
        elif line or has_subject:
            body = "\n".join(lines[index:]).lstrip()
            subjects = _values("\n".join(headers), "Betreff|Subject")
            if len(subjects) == 1 and body:
                return _quoted_mail(subjects[0], body, headers[:first_gap])
            break
        elif first_gap is None:
            first_gap = len(headers)
    raise MailParseError("Die zitierte Weiterleitung ist unvollständig.")


def _quoted_mail(subject: str, body: str, original_headers: list[str]) -> _InlineMail:
    headers = "\n".join(original_headers)
    senders = inline_addresses(headers, "Von|From")
    sender = normalize_contact_email(senders[0].addr_spec) if len(senders) == 1 else None
    if (
        _values(headers, "Betreff|Subject") != [subject]
        or not sender
        or FORWARD_PREFIX.match(subject)
        or REPLY_PREFIX.match(subject)
    ):
        return _InlineMail(subject, body, (), None, None)
    recipients = inline_addresses(headers, "An|To", outlook_recipients=True)
    displayed_names = _values(headers, "An|To")
    name_only = displayed_names[0] if len(displayed_names) == 1 and _name(displayed_names[0]) else None
    return _InlineMail(subject, body, recipients, sender, name_only)


def _body_identity(mail: _InlineMail) -> tuple | None:
    text = _clean_text(mail.text)
    event_type = _classify(mail.subject, text)
    participant = _participant_name(text, event_type, mail.subject)
    course = _unique(_values(text, "Lehrgangsart|Lehrgang"), _course_name)
    starts_at, ends_at = _period(text)
    venues = tuple(_values(text, LOCATION_LABELS))
    if event_type not in ("approval", "confirmation") or not all((participant, course, starts_at, ends_at, venues)):
        return None
    return (
        event_type, participant.casefold(), course.casefold(), starts_at, ends_at, venues,
        tuple(_values(text, "Buchungsreferenz|Buchungsnummer")),
    )


def _forward_original(message: EmailMessage, body: EmailMessage, primary: _InlineMail) -> _OriginalMail:
    source = "An-Feld der zitierten Originalmail"
    selected = _OriginalMail(primary.subject, _clean_text(primary.text), primary.recipients, source)
    if not primary.sender or body.get_content_type() != "text/plain":
        return selected

    # Only use a sibling HTML representation of the same forwarded mail.
    html = message.get_body(("html",))
    if html is None or not any(
        part.get_content_type() == "multipart/alternative"
        and body in part.get_payload() and html in part.get_payload()
        for part in message.walk()
    ):
        return selected
    try:
        alternate = _inline_forward(_body_text(html))
    except MailParseError:
        return selected
    if not alternate or not alternate.recipients or not alternate.sender:
        return selected

    plain_addresses = {normalize_contact_email(address.addr_spec) for address in primary.recipients}
    html_addresses = {normalize_contact_email(address.addr_spec) for address in alternate.recipients}
    if None in html_addresses:
        return selected
    same_envelope = primary.sender == alternate.sender and primary.subject.casefold() == alternate.subject.casefold()
    if plain_addresses:
        plain_identity, html_identity = _body_identity(primary), _body_identity(alternate)
        if (
            not same_envelope or plain_addresses != html_addresses
            or (plain_identity and html_identity and plain_identity != html_identity)
        ):
            return _OriginalMail(primary.subject, selected.text, conflict=True)
        return selected

    if same_envelope and primary.name_only_recipient and len(alternate.recipients) == 1:
        recipient = alternate.recipients[0]
        same_name = _normalized(recipient.display_name).casefold() == primary.name_only_recipient.casefold()
        if same_name and (identity := _body_identity(primary)) and identity == _body_identity(alternate):
            return _OriginalMail(primary.subject, selected.text, alternate.recipients, "An-Feld der HTML-Fassung der Originalmail")
    return selected


def _clean_text(text: str) -> str:
    return "\n".join(_normalized(line) for line in text.splitlines())


def _original_text(message: EmailMessage) -> _OriginalMail:
    subject = _normalized(str(message.get("Subject", "")))
    if REPLY_PREFIX.match(subject):
        return _OriginalMail(subject, "")
    forwarded = bool(FORWARD_PREFIX.match(subject))
    body = message.get_body(preferencelist=("plain", "html"))
    attached = _attached_messages(message)
    cleaned = ""
    if body is not None and not (forwarded and attached):
        text = _body_text(body)
        if original := _inline_forward(text):
            return _forward_original(message, body, original)
        cleaned = _clean_text(text)

    if attached and (body is None or forwarded or _classify(subject, cleaned) == "unknown"):
        if len(attached) > 1:
            raise MailParseError("Die Weiterleitung enthält mehrere E-Mails.")
        return _original_text(attached[0])

    if body is None:
        raise MailParseError("Die E-Mail enthält keinen lesbaren Klartext- oder HTML-Teil.")

    recipients = ()
    if not forwarded and len(message.get_all("Subject", ())) == 1:
        recipients = header_addresses([value for name, value in message.raw_items() if name.casefold() == "to"])
    subject = FORWARD_PREFIX.sub("", subject, count=1)
    if not cleaned.strip():
        raise MailParseError("Die E-Mail enthält keinen lesbaren Inhalt.")
    return _OriginalMail(subject, cleaned, recipients)


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


PARTICIPANT_LABELS = "Teilnehmer(?:in)?|Neue[rs]? Teilnehmer(?:in)?"


def _participant_name(text: str, event_type: str, subject: str) -> str | None:
    names = _values(text, PARTICIPANT_LABELS)
    if not names and event_type == "confirmation":
        names = re.findall(CONFIRMED_PARTICIPANT, text, re.IGNORECASE | re.MULTILINE)
        if not names and not re.match(PERSONAL_CONFIRMATION_SUBJECT, subject, re.IGNORECASE):
            return None
    if not names:
        names = re.findall(GREETING, text, re.IGNORECASE | re.MULTILINE)
    return _unique(names, _name)


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
        original = _original_text(message)
        subject, text = original.subject, original.text
    except (MessageError, RecursionError) as error:
        raise MailParseError("Die EML-Struktur kann nicht gelesen werden.") from error

    # Replies can contain HTML quotations and must not create new events or preview stale fields.
    is_reply = bool(REPLY_PREFIX.match(subject))
    if is_reply:
        subject, text = "", ""
    event_type = _classify(subject, text)
    participant_name = _participant_name(text, event_type, subject)
    selection = AddressSelection()
    if is_reply:
        selection = AddressSelection(note="Die Mail ist als Antwort gekennzeichnet. Leite die Original-Buchungsmail als EML oder mit ihren ursprünglichen Kopfzeilen weiter.")
    elif event_type in ("approval", "confirmation") or (
        event_type != "unknown" and original.recipients and _contact_subject(subject)
    ):
        if original.conflict:
            selection = AddressSelection(ambiguous=True, note="Klartext- und HTML-Fassung der Weiterleitung widersprechen sich bei Originalkopf oder Buchungsdaten.")
        elif not participant_name:
            selection = AddressSelection(note="Die Adresse konnte ohne eindeutig erkannten Teilnehmernamen nicht geprüft werden.")
        elif not original.recipients:
            selection = AddressSelection(note="Das ursprüngliche An-Feld fehlt oder konnte nicht eindeutig gelesen werden.")
        else:
            selection = select_participant_address(
                original.recipients, participant_name, functional_addresses,
                allow_name_in_address=event_type == "approval" or (
                    event_type == "confirmation" and bool(re.match(PERSONAL_CONFIRMATION_SUBJECT, subject, re.IGNORECASE))
                ),
                allow_approval_fallback=event_type == "approval" and subject.casefold() == BOOKING_SUBJECT,
            )
            if selection.email:
                selection = AddressSelection(email=selection.email, note=f"Quelle: {original.source}.")
    participant_email = selection.email
    participant_email_ambiguous = selection.ambiguous
    address_note = selection.note
    previous_name = _unique(_values(text, "Bisherige[rs]? Teilnehmer(?:in)?"), _name)
    course_name = _unique(_values(text, "Lehrgangsart|Lehrgang"), _course_name)
    references = re.findall(
        r"^(?:Buchungsreferenz|Buchungsnummer) *: *([^\n]*)$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    reference = _unique(references, _booking_reference)
    reference_conflict = bool(references) and reference is None
    venues = _values(text, LOCATION_LABELS)
    formats = _values(text, "Durchführungsform|Teilnahmeform|Unterrichtsform|Veranstaltungsform|Format")
    explicit_format = (*venues, *formats)
    title = course_name or ""
    is_online = not any(IN_PERSON_FORMAT.search(value) for value in (*explicit_format, title)) and (
        any(ONLINE_FORMAT.match(value) or ONLINE_SUFFIX.search(value) for value in explicit_format)
        or bool(ONLINE_COURSE.match(title) or ONLINE_SUFFIX.search(title))
    )
    locations = " ".join(venues)
    schools = [code for code, pattern in SCHOOLS.items() if re.search(pattern, locations, re.IGNORECASE)]
    school = schools[0] if len(schools) == 1 else None
    if is_online and not schools:
        school = "online"
    starts_at, ends_at = _period(text)
    expected_fields = (
        ("participant_name", PARTICIPANT_LABELS, participant_name),
        ("course_name", "Lehrgangsart|Lehrgang", course_name),
        ("school", LOCATION_LABELS, school),
        ("period", "Lehrgangstermin|Zeitraum", starts_at),
    )
    unusable_fields = tuple(
        label for label, pattern, value in expected_fields
        if value is None and re.search(rf"^(?:{pattern}) *:", text, re.IGNORECASE | re.MULTILINE)
    )
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
        participant_email_candidate=selection.candidate if event_type in ("approval", "confirmation") else None,
        participant_email_ambiguous=participant_email_ambiguous,
        address_note=address_note,
        sent_at=_sent_at(message),
        is_online=is_online,
        unusable_fields=unusable_fields,
    )
