from collections.abc import Collection

from mailimport.email_address import normalize_contact_email

from .models import CourseSeat, School


def booking_missing_fields(seat: CourseSeat, *, functional_addresses: Collection[str]) -> tuple[str, ...]:
    participant = seat.participant
    session = seat.session
    email = normalize_contact_email(participant.email) if participant else None
    missing = []
    if not participant or not participant.name.strip():
        missing.append("Name")
    if not email or email in functional_addresses:
        missing.append("persönliche E-Mail-Adresse")
    if not session.course.name.strip():
        missing.append("Lehrgang")
    if session.school not in School.values:
        missing.append("Schulungsort")
    if not session.starts_at or not session.ends_at or session.ends_at <= session.starts_at:
        missing.append("Zeitraum")
    return tuple(missing)
