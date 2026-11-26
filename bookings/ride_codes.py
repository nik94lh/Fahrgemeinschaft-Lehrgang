"""Personal access codes for the participation portal."""

import re
from base64 import b32encode, urlsafe_b64encode
from hashlib import sha256

from django.utils.crypto import salted_hmac


_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ENCODE = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567", _ALPHABET)
_TYPED_ALIASES = str.maketrans({"I": "1", "L": "1", "O": "0"})
_LEGACY_CODE = re.compile(r"[A-Za-z0-9_-]{43}")
_SHORT_CODE = re.compile(rf"[{_ALPHABET}]{{16}}")


def code_digest(code):
    return sha256(code.encode("ascii")).hexdigest()


def compact_code_v1(seed):
    """Encode 80 bits of the keyed digest. This format is used by the data migration."""
    key = salted_hmac("bookings.ride_participation.code.v1", str(seed), algorithm="sha256").digest()
    return b32encode(key[:10]).decode("ascii").translate(_ENCODE)


def legacy_code(seed):
    key = salted_hmac("bookings.ride_participation.code", str(seed), algorithm="sha256").digest()
    return urlsafe_b64encode(key).decode("ascii").rstrip("=")


def normalize_code(value):
    if _LEGACY_CODE.fullmatch(value):
        return value
    if not value.isascii():
        return None
    code = value.replace("-", "").replace(" ", "").upper().translate(_TYPED_ALIASES)
    return code if _SHORT_CODE.fullmatch(code) else None


def display_code(participation):
    code = compact_code_v1(participation.token_seed)
    if code_digest(code) != participation.short_token_digest:
        return legacy_code(participation.token_seed)
    return "-".join(code[offset:offset + 4] for offset in range(0, len(code), 4))
