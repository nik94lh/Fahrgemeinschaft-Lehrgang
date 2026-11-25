"""Stored configuration for the optional, read-only mailbox worker."""

import base64
import hmac
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.views.decorators.debug import sensitive_variables

from .models import MailboxConfiguration


class MailboxSettingsError(ValueError):
    """Stored mailbox settings cannot be used."""


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
def load_mailbox_settings() -> MailboxSettings | None:
    """Prefer a saved configuration, including an explicitly disabled one."""

    record = MailboxConfiguration.objects.filter(pk=1).first()
    if record is None:
        return None

    password = ""
    if record.enabled:
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
        enabled=record.enabled,
    )
