import re


BOOKING_ID_WITH_LABEL = re.compile(r"Buchungs(?:-|\s+)ID(?:\s*:\s*|\s+)([0-9]+)", re.IGNORECASE)


def positive_integer(value):
    if value and value.isascii() and value.isdecimal() and len(value) <= 19:
        number = int(value)
        if 0 < number <= (1 << 63) - 1:
            return number
    return None


def booking_id_query(value):
    """Identify an explicit internal booking ID or a numeric text query."""
    match = BOOKING_ID_WITH_LABEL.fullmatch(value)
    if match:
        return True, positive_integer(match[1])
    return False, positive_integer(value)
