"""Opt-in SMTP transport for queued ride messages."""

import base64
import hashlib
import hmac
import json
import os
import re
import smtplib
import socket
import ssl
from contextlib import suppress
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import format_datetime

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .email_address import normalize_contact_email
from .imap_network import resolve_public_host, validate_public_hostname
from .mailbox_settings import _cipher as _imap_cipher
from .models import MailboxConfiguration, OutboundMailConfiguration


_TIMEOUT = 10
_MESSAGE_ID = re.compile(r"<[A-Za-z0-9._%+:-]+@[A-Za-z0-9.-]+>")
_INPUT_SLOTS = {
    OutboundMailConfiguration.SenderSource.INPUT_ONE: 1,
    OutboundMailConfiguration.SenderSource.INPUT_TWO: 2,
}


class SMTPDeliveryError(RuntimeError):
    """SMTP could not accept the message; safe to display or store this error."""


class SMTPDeliveryUncertain(SMTPDeliveryError):
    """The connection was lost before the result of sending was known."""


@dataclass(frozen=True, slots=True)
class SenderCredentials:
    username: str
    password: str = field(repr=False)
    identity: str = ""
    source_host: str = ""


@dataclass(frozen=True, slots=True)
class _Sender:
    host: str
    port: int
    security: str
    sender_address: str
    credentials: SenderCredentials


def valid_smtp_login(value):
    return bool(value) and all(" " <= character <= "~" for character in value)


@sensitive_variables()
def _smtp_cipher():
    key = hmac.digest(settings.SECRET_KEY.encode("utf-8"), b"bms:smtp-password-encryption:v1", "sha256")
    return Fernet(base64.urlsafe_b64encode(key))


@sensitive_variables()
def encrypt_smtp_password(password):
    return _smtp_cipher().encrypt(password.encode("utf-8")).decode("ascii")


@sensitive_variables()
def decrypt_smtp_password(ciphertext):
    try:
        return _smtp_cipher().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError):
        return ""


@sensitive_variables()
def linked_sender_credentials(source):
    """Read explicitly selected input credentials without enabling inbound polling."""
    slot = _INPUT_SLOTS.get(source)
    if slot is None:
        return None

    record = MailboxConfiguration.objects.filter(pk=slot).first()
    if record is not None:
        host, port, username = record.host, str(record.port), record.username
        try:
            password = _imap_cipher().decrypt(record.encrypted_password.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError):
            return None
    elif slot == 1:
        host = os.environ.get("BMS_IMAP_HOST", "")
        port = os.environ.get("BMS_IMAP_PORT", "993")
        username = os.environ.get("BMS_IMAP_USERNAME", "")
        password = os.environ.get("BMS_IMAP_PASSWORD", "")
    else:
        return None

    host = host.strip().rstrip(".").lower()
    if not host or not valid_smtp_login(username) or not valid_smtp_login(password):
        return None
    identity = hashlib.sha256(json.dumps([slot, host, port, username]).encode("ascii")).hexdigest()
    return SenderCredentials(username, password, identity=identity, source_host=host)


def linked_smtp_host_allowed(host, source_host):
    return host == source_host or host in getattr(settings, "BMS_SMTP_LINKED_HOSTS", ())


@sensitive_variables()
def _configured_sender(*, automatic=False):
    if not getattr(settings, "BMS_SMTP_ENABLED", False):
        return None
    config = OutboundMailConfiguration.objects.filter(pk=1, enabled=True).first()
    if config is None or (automatic and not config.allow_automatic_inquiries):
        return None
    try:
        host = validate_public_hostname(config.host)
        if config.sender_source == OutboundMailConfiguration.SenderSource.SEPARATE:
            password = decrypt_smtp_password(config.encrypted_password)
            credentials = SenderCredentials(config.username, password)
        else:
            credentials = linked_sender_credentials(config.sender_source)
            if (
                credentials is None or credentials.identity != config.linked_identity
                or not linked_smtp_host_allowed(host, credentials.source_host)
            ):
                return None
    except ValueError:
        return None
    address = normalize_contact_email(config.sender_address)
    if (
        not address or not 1 <= config.port <= 65535 or config.security not in OutboundMailConfiguration.Security.values
        or not valid_smtp_login(credentials.username) or not valid_smtp_login(credentials.password)
    ):
        return None
    return _Sender(host, config.port, config.security, address, credentials)


def outbound_enabled() -> bool:
    return _configured_sender() is not None


def outbound_automation_enabled() -> bool:
    return _configured_sender(automatic=True) is not None


class _SMTPConnection(smtplib.SMTP):
    def __init__(self, host, port, *, addresses, tls_context=None):
        self.addresses = addresses
        self.tls_context = tls_context
        super().__init__(host, port, timeout=_TIMEOUT)

    def _get_socket(self, host, port, timeout):
        # Connect to the validated addresses and keep the configured hostname for TLS.
        for family, kind, protocol, address in self.addresses[:4]:
            try:
                connection = socket.socket(family, kind, protocol)
            except OSError:
                continue
            try:
                connection.settimeout(timeout)
                connection.connect(address)
            except OSError:
                connection.close()
                continue
            if self.tls_context is None:
                return connection
            try:
                return self.tls_context.wrap_socket(connection, server_hostname=host)
            except OSError:
                connection.close()
                raise
        raise OSError("Keine geprüfte Adresse des SMTP-Servers ist erreichbar.")


@sensitive_variables()
def _send_message(connection, message, sender, recipient):
    try:
        refused = connection.send_message(message, from_addr=sender, to_addrs=[recipient])
    except smtplib.SMTPServerDisconnected:
        raise SMTPDeliveryUncertain("Es liegt keine Versandbestätigung des SMTP-Servers vor.") from None
    except smtplib.SMTPException:
        raise
    except OSError:
        raise SMTPDeliveryUncertain("Es liegt keine Versandbestätigung des SMTP-Servers vor.") from None
    if refused:
        raise SMTPDeliveryError("Der SMTP-Server hat die Empfängeradresse abgelehnt.")


@sensitive_variables()
def send_ride_mail(*, recipient: str, subject: str, body: str, message_id: str) -> None:
    """Send exactly one queued message and return once SMTP accepts it."""
    sender = _configured_sender()
    if sender is None:
        raise SMTPDeliveryError("Der SMTP-Versand ist deaktiviert oder die Zugangsdaten sind nicht verfügbar.")
    recipient = normalize_contact_email(recipient)
    if not recipient or recipient.rpartition("@")[2].endswith(".invalid"):
        raise SMTPDeliveryError("Die Empfängeradresse ist für den SMTP-Versand ungültig.")
    if not _MESSAGE_ID.fullmatch(message_id):
        raise SMTPDeliveryError("Die Nachrichtenkennung ist ungültig.")

    connection = None
    try:
        message = EmailMessage()
        message["From"] = sender.sender_address
        message["To"] = recipient
        message["Subject"] = subject
        message["Message-ID"] = message_id
        message["Date"] = format_datetime(timezone.now())
        message.set_content(body)

        addresses = resolve_public_host(sender.host, sender.port)
        context = ssl.create_default_context()
        implicit_tls = sender.security == OutboundMailConfiguration.Security.TLS
        connection = _SMTPConnection(
            sender.host, sender.port, addresses=addresses, tls_context=context if implicit_tls else None,
        )
        if not implicit_tls:
            connection.starttls(context=context)
        connection.login(sender.credentials.username, sender.credentials.password)
        _send_message(connection, message, sender.sender_address, recipient)
    except smtplib.SMTPAuthenticationError:
        raise SMTPDeliveryError("Die SMTP-Anmeldung ist fehlgeschlagen.") from None
    except smtplib.SMTPRecipientsRefused:
        raise SMTPDeliveryError("Der SMTP-Server hat die Empfängeradresse abgelehnt.") from None
    except (OSError, ValueError, TypeError):
        raise SMTPDeliveryError("Der SMTP-Server konnte den Versand nicht bestätigen.") from None
    finally:
        if connection is not None:
            with suppress(OSError):
                connection.quit()
            with suppress(OSError):
                connection.close()
