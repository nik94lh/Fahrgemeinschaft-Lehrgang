from django import forms
from django.conf import settings
from django.views.decorators.debug import sensitive_variables

from .email_address import normalize_contact_email
from .imap_network import validate_public_hostname
from .models import OutboundMailConfiguration
from .outbound import (
    decrypt_smtp_password, encrypt_smtp_password, linked_sender_credentials, linked_smtp_host_allowed, valid_smtp_login,
)


OUTBOUND_FIELDS = (
    "sender_source", "host", "port", "security", "sender_address", "username", "enabled", "allow_automatic_inquiries",
)
_SAME_ACCOUNT_FIELDS = ("sender_source", "host", "port", "security", "username")


class OutboundMailForm(forms.Form):
    sender_source = forms.ChoiceField(
        label="Versandkonto", choices=OutboundMailConfiguration.SenderSource.choices,
        initial=OutboundMailConfiguration.SenderSource.SEPARATE,
    )
    host = forms.CharField(
        label="SMTP-Server", max_length=253,
        help_text="Für Zugangsdaten eines Eingangs verwende dessen Hostnamen oder einen serverseitig freigegebenen SMTP-Server.",
    )
    port = forms.IntegerField(label="SMTP-Port", min_value=1, max_value=65535, initial=587)
    security = forms.ChoiceField(
        label="Verbindungssicherheit", choices=OutboundMailConfiguration.Security.choices,
        initial=OutboundMailConfiguration.Security.STARTTLS,
    )
    sender_address = forms.EmailField(label="Absenderadresse", max_length=254)
    username = forms.CharField(label="Benutzername des separaten Versandkontos", max_length=255, required=False, strip=False)
    password = forms.CharField(
        label="Passwort des separaten Versandkontos", max_length=1024, required=False, strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Leer lassen, um das gespeicherte Passwort für dieselbe SMTP-Verbindung zu behalten.",
    )
    enabled = forms.BooleanField(
        label="SMTP-Versand aktivieren", required=False,
        help_text="Erlaubt den Versand echter E-Mails an Teilnehmende, wenn der Server den Versand freigibt.",
    )
    allow_automatic_inquiries = forms.BooleanField(
        label="Automatische Teilnahmeabfragen für echte Buchungen erlauben", required=False,
        help_text="Der automatische Start erfordert aktiven SMTP-Versand und die eingeschaltete Automatik in BMS.",
    )

    def __init__(self, *args, configuration=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.configuration = configuration
        if configuration is not None and not self.is_bound:
            self.initial.update({name: getattr(configuration, name) for name in OUTBOUND_FIELDS})

    def clean_host(self):
        try:
            return validate_public_hostname(self.cleaned_data["host"])
        except ValueError:
            raise forms.ValidationError("Gib den öffentlichen Hostnamen des SMTP-Servers ein.") from None

    def clean_sender_address(self):
        address = normalize_contact_email(self.cleaned_data["sender_address"])
        if not address:
            raise forms.ValidationError("Gib eine gültige Absenderadresse ein.")
        return address

    @sensitive_variables()
    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        enabled = data["enabled"]
        if enabled and not getattr(settings, "BMS_SMTP_ENABLED", False):
            self.add_error("enabled", "Der SMTP-Versand ist in der Serverkonfiguration gesperrt.")

        data["linked_identity"] = ""
        data["encrypted_password"] = ""
        if data["sender_source"] != OutboundMailConfiguration.SenderSource.SEPARATE:
            credentials = linked_sender_credentials(data["sender_source"])
            if credentials is not None:
                if linked_smtp_host_allowed(data["host"], credentials.source_host):
                    data["linked_identity"] = credentials.identity
                else:
                    self.add_error("host", "Für diesen SMTP-Server fehlt die Freigabe zur Verwendung der Zugangsdaten des Eingangs.")
            elif enabled:
                self.add_error("sender_source", "Für diesen Eingang fehlen nutzbare Zugangsdaten.")
            data["username"] = ""
            return data

        username, password = data["username"], data["password"]
        if (enabled or username) and not valid_smtp_login(username):
            self.add_error("username", "Gib den Benutzernamen in druckbaren ASCII-Zeichen ein.")
        if password and not valid_smtp_login(password):
            self.add_error("password", "Verwende druckbare ASCII-Zeichen für das Passwort.")
            return data
        old = self.configuration
        same_account = old is not None and all(getattr(old, name) == data.get(name) for name in _SAME_ACCOUNT_FIELDS)
        if password:
            data["encrypted_password"] = encrypt_smtp_password(password)
        elif same_account:
            data["encrypted_password"] = old.encrypted_password
        if enabled and not password and not decrypt_smtp_password(data["encrypted_password"]):
            self.add_error("password", "Gib das Passwort des SMTP-Versandkontos ein.")
        return data
