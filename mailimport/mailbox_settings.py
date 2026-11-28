"""Stored configuration for the optional, read-only mailbox worker."""

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.views.decorators.debug import sensitive_variables

from .models import MailboxConfiguration


class MailboxSettingsError(ValueError):
    """Stored mailbox settings cannot be used."""


def mailbox_source_id(host: str, port: int, username: str, mailbox: str) -> str:
    identity = (host.lower().rstrip("."), port, username, mailbox)
    return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class MailboxSettings:
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    mailbox: str
    enabled: bool


@sensitive_variables()
def _cipher() -> Fernet:
    key = hmac.digest(
        settings.SECRET_KEY.encode("utf-8"), b"bms:imap-password-encryption:v1", "sha256",
    )
    return Fernet(base64.urlsafe_b64encode(key))


@sensitive_variables()
def encrypt_password(password: str) -> str:
    return _cipher().encrypt(password.encode("utf-8")).decode("ascii")


@sensitive_variables()
def load_mailbox_settings(slot=MailboxConfiguration.Slot.FIRST, *, only_source_id=None) -> MailboxSettings | None:
    """Prefer a saved configuration, including an explicitly disabled one."""

    record = MailboxConfiguration.objects.filter(pk=slot).first()
    if record is None:
        return None

    selected = only_source_id is None or only_source_id == mailbox_source_id(
        record.host, record.port, record.username, record.mailbox,
    )
    enabled = record.enabled and selected
    password = ""
    if enabled:
        try:
            password = _cipher().decrypt(record.encrypted_password.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError):
            raise MailboxSettingsError("Das gespeicherte Postfachpasswort ist nicht lesbar.") from None
    return MailboxSettings(
        host=record.host,
        port=record.port,
        username=record.username,
        password=password,
        mailbox=record.mailbox,
        enabled=enabled,
    )
