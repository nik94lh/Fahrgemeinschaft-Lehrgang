"""Read-only bounded import from an explicitly configured IMAP mailbox."""

import hashlib
import imaplib
import json
import os
import re
import ssl
from contextlib import contextmanager
from dataclasses import dataclass, field

from django.db import DatabaseError, transaction
from django.utils import timezone

from bookings.services import ingest

from .imap_network import BoundedIMAP4_SSL, resolve_public_host, validate_public_hostname
from .mailbox_settings import load_mailbox_settings
from .models import MailboxCursor
from .parser import MAX_EML_BYTES, MailParseError


DEFAULT_BATCH_SIZE = 25
MAX_BATCH_SIZE = 100
SEARCH_WINDOW = 1000
MAX_UID = (1 << 32) - 1

_NUMBER = re.compile(rb"[0-9]{1,10}")
_UID = re.compile(rb"\bUID ([0-9]{1,10})(?= |\))", re.IGNORECASE)
_SIZE = re.compile(rb"\bRFC822\.SIZE ([0-9]{1,18})(?= |\))", re.IGNORECASE)
_BODY_LITERAL = re.compile(rb"\bBODY\[\] \{([0-9]{1,10})\}$", re.IGNORECASE)
_BODY_TRAILER = re.compile(rb" *(?:UID [0-9]{1,10} *)?\)", re.IGNORECASE)


class ImapSyncError(RuntimeError):
    """A sanitized failure that can be shown to the mailbox operator."""


def mailbox_source_id(host: str, port: int, username: str, mailbox: str) -> str:
    identity = (host.lower().rstrip("."), port, username, mailbox)
    return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class ImapConfig:
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    mailbox: str = "INBOX"
    pin_public_host: bool = False

    @property
    def source_id(self) -> str:
        return mailbox_source_id(self.host, self.port, self.username, self.mailbox)


@dataclass(frozen=True, slots=True)
class SyncSummary:
    processed: int
    skipped: int
    has_more: bool


def _printable_ascii(value: str) -> bool:
    return bool(re.fullmatch(r"[ -~]+", value))


def load_configuration() -> ImapConfig | None:
    try:
        settings = load_mailbox_settings()
        if settings is not None:
            if not settings.enabled:
                return None
            config = ImapConfig(
                host=validate_public_hostname(settings.host),
                port=settings.port,
                username=settings.username,
                password=settings.password,
                mailbox=settings.mailbox,
                pin_public_host=True,
            )
        elif os.environ.get("BMS_IMAP_ENABLED") == "1":
            config = ImapConfig(
                host=os.environ.get("BMS_IMAP_HOST", "").strip(),
                port=int(os.environ.get("BMS_IMAP_PORT", "993")),
                username=os.environ.get("BMS_IMAP_USERNAME", ""),
                password=os.environ.get("BMS_IMAP_PASSWORD", ""),
                mailbox=os.environ.get("BMS_IMAP_MAILBOX", "INBOX"),
            )
        else:
            return None
    except (ValueError, DatabaseError):
        raise ImapSyncError("Die IMAP-Einstellungen konnten nicht geladen werden.") from None

    if not 1 <= config.port <= 65535 or not all(
        _printable_ascii(value) for value in (config.host, config.username, config.password, config.mailbox)
    ):
        raise ImapSyncError("Die IMAP-Einstellungen sind unvollständig oder ungültig.")
    return config


def _open_mailbox(config):
    try:
        addresses = resolve_public_host(config.host, config.port) if config.pin_public_host else None
    except (OSError, ValueError):
        raise ImapSyncError("Der IMAP-Hostname konnte nicht als öffentliches Ziel bestätigt werden.") from None
    return BoundedIMAP4_SSL(
        config.host,
        config.port,
        ssl_context=ssl.create_default_context(),
        timeout=10,
        pinned_addresses=addresses,
    )


def _quoted(value: str) -> str:
    if not _printable_ascii(value):
        raise ImapSyncError("Benutzername oder Ordner enthält nicht unterstützte Zeichen.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _require_ok(response):
    status, payload = response
    if status != "OK":
        raise ImapSyncError("Der IMAP-Server hat einen Lesevorgang abgelehnt.")
    return payload


@contextmanager
def _connect_readonly(config: ImapConfig):
    with _open_mailbox(config) as client:
        _require_ok(client.login(_quoted(config.username), config.password))
        _require_ok(client.select(_quoted(config.mailbox), readonly=True))
        yield client


def _response_number(client, name: str) -> int:
    _, data = client.response(name)
    if not data or not isinstance(data[0], bytes) or not _NUMBER.fullmatch(data[0]):
        raise ImapSyncError("Der IMAP-Server hat keine gültige UID-Information geliefert.")
    number = int(data[0])
    if not 1 <= number <= MAX_UID:
        raise ImapSyncError("Der IMAP-Server hat keine gültige UID-Information geliefert.")
    return number


def _search(client, start: int, end: int) -> list[int]:
    data = _require_ok(client.uid("SEARCH", None, "UID", f"{start}:{end}"))
    if not data or len(data) != 1 or not isinstance(data[0], bytes):
        raise ImapSyncError("Die IMAP-Suche hat ungültige Daten geliefert.")
    numbers = data[0].split()
    if len(numbers) > SEARCH_WINDOW or not all(_NUMBER.fullmatch(token) for token in numbers):
        raise ImapSyncError("Die IMAP-Suche hat ungültige Daten geliefert.")
    uids = {int(token) for token in numbers}
    if any(not start <= uid <= end for uid in uids):
        raise ImapSyncError("Die IMAP-Suche hat UIDs außerhalb des erwarteten Bereichs geliefert.")
    return sorted(uids)


def _fetch_size(client, uid: int) -> int | None:
    payload = _require_ok(client.uid("FETCH", str(uid), "(UID RFC822.SIZE)"))
    if not any(payload):
        return None
    sizes = []
    for entry in payload:
        if not isinstance(entry, bytes):
            raise ImapSyncError("Die IMAP-Größenabfrage enthielt unerwartete Daten.")
        entry_uid = _UID.search(entry)
        size = _SIZE.search(entry)
        if entry_uid and size and int(entry_uid[1]) == uid:
            sizes.append(int(size[1]))
    if len(sizes) != 1:
        raise ImapSyncError("Die IMAP-Größenabfrage enthielt keine eindeutige Nachricht.")
    return sizes[0]


def _fetch_body(client, uid: int, expected_size: int) -> bytes | None:
    payload = _require_ok(client.uid("FETCH", str(uid), "(UID BODY.PEEK[])"))
    if not any(payload):
        return None
    literals = [(index, item) for index, item in enumerate(payload) if isinstance(item, tuple)]
    if len(literals) != 1:
        raise ImapSyncError("Die IMAP-Abfrage enthielt keine eindeutige Nachricht.")
    index, (prefix, raw) = literals[0]
    trailer = payload[index + 1] if index + 1 < len(payload) else None
    if not isinstance(trailer, bytes) or not _BODY_TRAILER.fullmatch(trailer):
        raise ImapSyncError("Die IMAP-Abfrage war unvollständig.")
    size = _BODY_LITERAL.search(prefix)
    uids = _UID.findall(prefix + b" " + trailer)
    if not size or uids != [str(uid).encode()] or not isinstance(raw, bytes):
        raise ImapSyncError("Die IMAP-Abfrage enthielt keine eindeutige Nachricht.")
    if len(raw) != int(size[1]) or len(raw) != expected_size or len(raw) > MAX_EML_BYTES:
        raise ImapSyncError("Die IMAP-Nachricht hat eine unerwartete Größe.")
    return raw


def _read_uid(client, uid: int) -> bytes | None:
    size = _fetch_size(client, uid)
    if size is None or size == 0 or size > MAX_EML_BYTES:
        return None
    return _fetch_body(client, uid, size)


def _import_uid(client, uid: int) -> bool:
    raw = _read_uid(client, uid)
    if raw is None:
        return False
    try:
        # UID order and arrival time of forwarded copies are not original event times.
        ingest(raw, occurred_at=None)
    except MailParseError:
        return False
    return True


@transaction.atomic
def _sync_selected(client, config: ImapConfig, limit: int) -> SyncSummary:
    uid_validity = _response_number(client, "UIDVALIDITY")
    newest_uid = _response_number(client, "UIDNEXT") - 1
    cursor, _ = MailboxCursor.objects.select_for_update().get_or_create(
        source_id=config.source_id,
        defaults={"uid_validity": uid_validity},
    )
    if cursor.uid_validity != uid_validity:
        cursor.uid_validity = uid_validity
        cursor.last_uid = 0
    if newest_uid < cursor.last_uid:
        raise ImapSyncError("Der IMAP-Server meldet einen widersprüchlichen UID-Bereich.")

    first_uid = cursor.last_uid + 1
    last_uid = min(newest_uid, cursor.last_uid + SEARCH_WINDOW)
    uids = _search(client, first_uid, last_uid) if first_uid <= last_uid else []
    processed = sum(_import_uid(client, uid) for uid in uids[:limit])
    cursor.last_uid = uids[limit - 1] if len(uids) > limit else last_uid
    cursor.last_sync_at = timezone.now()
    cursor.last_sync_ok = True
    cursor.save(update_fields=("uid_validity", "last_uid", "last_sync_at", "last_sync_ok"))
    return SyncSummary(processed, min(limit, len(uids)) - processed, cursor.last_uid < newest_uid)


def sync_mailbox(config: ImapConfig, *, limit: int = DEFAULT_BATCH_SIZE) -> SyncSummary:
    if not 1 <= limit <= MAX_BATCH_SIZE:
        raise ImapSyncError("Das Abruflimit muss zwischen 1 und 100 liegen.")
    try:
        with _connect_readonly(config) as client:
            return _sync_selected(client, config, limit)
    except (ImapSyncError, OSError, imaplib.IMAP4.error, DatabaseError) as error:
        try:
            MailboxCursor.objects.update_or_create(
                source_id=config.source_id,
                defaults={"last_sync_at": timezone.now(), "last_sync_ok": False},
            )
        except DatabaseError:
            pass
        if isinstance(error, ImapSyncError):
            raise
        raise ImapSyncError("Der IMAP-Abruf ist fehlgeschlagen.") from None
