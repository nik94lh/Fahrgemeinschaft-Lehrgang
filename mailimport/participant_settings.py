"""Explicit functional recipient addresses used for participant recognition."""

from .email_address import normalize_contact_email
from .models import ParticipantRecognitionConfiguration


def get_functional_addresses() -> tuple[str, ...]:
    addresses = ParticipantRecognitionConfiguration.objects.filter(pk=1).values_list(
        "functional_addresses", flat=True,
    ).first()
    if not isinstance(addresses, list):
        return ()

    normalized = {}
    for value in addresses:
        if isinstance(value, str) and (address := normalize_contact_email(value)):
            normalized[address] = None
    return tuple(normalized)
