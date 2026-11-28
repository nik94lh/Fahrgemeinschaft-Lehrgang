"""Weekly ride dates derived from the original course in Berlin time."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import CourseSession


BERLIN = ZoneInfo("Europe/Berlin")


@dataclass(frozen=True, slots=True)
class RideSegment:
    departure_day: date
    return_day: date
    starts_at: datetime
    ends_at: datetime
    start_known: bool
    end_known: bool


def ride_segments(session: CourseSession) -> tuple[RideSegment, ...]:
    """Split ride dates around full weekends between course days."""
    first_day = session.starts_at.astimezone(BERLIN).date()
    last_day = session.ends_at.astimezone(BERLIN).date()
    departure_day, starts_at = first_day, session.starts_at
    segments = []
    length = (last_day - first_day).days
    # Look for a Saturday after the recorded start date.
    first_saturday = (5 - first_day.weekday()) % 7 or 7
    for saturday in range(first_saturday, length - 1, 7):
        friday = first_day + timedelta(days=saturday - 1)
        monday = first_day + timedelta(days=saturday + 2)
        continuation = datetime.combine(monday, time.min, tzinfo=BERLIN)
        if continuation >= session.ends_at:
            break
        segments.append(RideSegment(
            departure_day, friday, starts_at, datetime.combine(friday, time.max, tzinfo=BERLIN),
            start_known=departure_day == first_day, end_known=False,
        ))
        departure_day, starts_at = monday, continuation
    segments.append(RideSegment(
        departure_day, last_day, starts_at, session.ends_at,
        start_known=departure_day == first_day, end_known=True,
    ))
    return tuple(segments)
