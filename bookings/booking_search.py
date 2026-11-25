import re

from django.urls import reverse

from .models import BookingStatus, School


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


def booking_filters(parameters):
    filters = {}
    query = parameters.get("q", "").strip()[:100]
    school = parameters.get("school", "")
    status = parameters.get("status", "")
    if query:
        filters["q"] = query
    if school in School.values:
        filters["school"] = school
    if status == "pending" or status in BookingStatus.values:
        filters["status"] = status
    return filters


def booking_list_navigation(parameters):
    list_params = booking_filters(parameters)
    detail_params = list_params.copy()
    list_page = positive_integer(parameters.get("list_page"))
    if list_page and list_page > 1:
        list_params["page"] = list_page
        detail_params["list_page"] = list_page
    return reverse("bookings", query=list_params), detail_params
