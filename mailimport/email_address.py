"""Canonical syntax checks for mail addresses used as participant identities."""

import re

import idna


_EMAIL_LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+", re.ASCII)
_DOMAIN_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_EMAIL_DOMAIN = re.compile(rf"(?:{_DOMAIN_LABEL}\.)+{_DOMAIN_LABEL}", re.ASCII)


def normalize_contact_email(value: str) -> str | None:
    local, separator, domain = value.strip().rpartition("@")
    if (
        not separator
        or len(local) > 64
        or len(domain) > 253
        or not _EMAIL_LOCAL.fullmatch(local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
    ):
        return None
    try:
        ascii_domain = idna.encode(domain, uts46=True, std3_rules=True).decode("ascii")
    except idna.IDNAError:
        return None

    if len(local) + len(ascii_domain) + 1 > 254 or not _EMAIL_DOMAIN.fullmatch(ascii_domain):
        return None
    return f"{local}@{ascii_domain}".casefold()
