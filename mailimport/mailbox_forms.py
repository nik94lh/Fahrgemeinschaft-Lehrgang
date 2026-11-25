from django import forms
from django.views.decorators.debug import sensitive_variables

from .email_address import normalize_contact_email
from .imap_network import validate_public_hostname
from .mailbox_settings import encrypt_password


def _is_printable_ascii(value: str) -> bool:
    return all(32 <= ord(character) <= 126 for character in value)


class MailboxForm(forms.Form):
    host = forms.CharField(
        label="IMAP-Server",
        max_length=253,
        widget=forms.TextInput(attrs={"placeholder": "imap.beispiel.de", "autocomplete": "off"}),
    )
    port = forms.IntegerField(label="TLS-Port", min_value=1, max_value=65535, initial=993)
    username = forms.CharField(label="Benutzername", max_length=255, strip=False)
    mailbox = forms.CharField(label="Ordner", max_length=255, initial="INBOX", strip=False)
    password = forms.CharField(
        label="Postfachpasswort",
        max_length=1024,
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Leer lassen, um das gespeicherte Passwort bei unverändertem Postfach zu behalten.",
    )
    enabled = forms.BooleanField(label="Lesenden Postfachabruf aktivieren", required=False)

    def __init__(self, *args, configuration=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.configuration = configuration
        if configuration is not None and not self.is_bound:
            self.initial.update({
                name: getattr(configuration, name)
                for name in ("host", "port", "username", "mailbox", "enabled")
            })

    def clean_host(self):
        try:
            return validate_public_hostname(self.cleaned_data["host"])
        except ValueError:
            raise forms.ValidationError("Gib den öffentlichen Hostnamen des IMAP-Servers ein.") from None

    @sensitive_variables()
    def clean(self):
        values = super().clean()
        for name in ("username", "mailbox", "password"):
            if not _is_printable_ascii(values.get(name, "")):
                self.add_error(name, "Erlaubt sind druckbare ASCII-Zeichen.")

        if self.errors:
            return values

        old = self.configuration
        same_mailbox = old is not None and all(
            getattr(old, key) == values[key] for key in ("host", "port", "username", "mailbox")
        )
        stored_password = old.encrypted_password if same_mailbox else ""
        if values["password"]:
            stored_password = encrypt_password(values["password"])
        if values["enabled"] and not stored_password:
            self.add_error("password", "Bitte gib das Passwort für dieses Postfach ein.")
        values["encrypted_password"] = stored_password
        return values


class FunctionalAddressesForm(forms.Form):
    functional_addresses = forms.CharField(
        label="Funktionsadressen",
        required=False,
        max_length=12000,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 4, "autocomplete": "off", "spellcheck": "false"}),
        help_text="Eine vollständige E-Mail-Adresse pro Zeile. Zum Entfernen alle Zeilen leeren.",
    )

    def clean_functional_addresses(self):
        field = forms.EmailField(max_length=254)
        addresses = {}
        for line_number, line in enumerate(self.cleaned_data["functional_addresses"].splitlines(), start=1):
            if not line.strip():
                continue
            try:
                address = normalize_contact_email(field.clean(line))
            except forms.ValidationError:
                address = None
            if address is None:
                raise forms.ValidationError(
                    "Zeile %(line)s enthält keine gültige E-Mail-Adresse.",
                    params={"line": line_number},
                )
            addresses[address] = None
        return list(addresses)
