"""Operator views for persisted ride matches."""
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from django import forms
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Prefetch, Q, prefetch_related_objects
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.access import can_edit

from .models import RideCase, RideNotice, RideParticipation
from .ride_inquiry_views import get_ride_automation, inquiry_controls_available
from .ride_participation import _has_access, _open_test_case, notice_preview
from .ride_settings import get_ride_lead_days
from .rides_matching import BERLIN, set_ride_case_status, sync_ride_cases


RIDE_PHASE_CHOICES = {"ready": "Bereit zur Vermittlung", "standby": "Standby"}
INQUIRY_MESSAGES = {
    "vorbereitet": "Testabfrage vorbereitet. Die Einladungen stehen als Vorschau bereit.",
    "unveraendert": "Für diesen Vorschlag sind bereits Testeinladungen vorbereitet.",
    "nicht-moeglich": "Die Testabfrage lässt sich für diesen Vorschlag derzeit nicht starten.",
}
INQUIRY_ANSWER_LABELS = {
    RideParticipation.Answer.PENDING: "Einladung vorbereitet",
    RideParticipation.Answer.YES: "Zugesagt",
    RideParticipation.Answer.NO: "Abgesagt",
}


class RideCaseStatusForm(forms.Form):
    status = forms.ChoiceField(label="Bearbeitungsstand", choices=RideCase.Status.choices)
    expected_status = forms.ChoiceField(choices=RideCase.Status.choices, widget=forms.HiddenInput)


def _list_filters(params, *, archived=False, include_page=False):
    filters = {"archiv": "1"} if archived else {}
    status = params.get("status", "")
    phase = params.get("phase", "")
    if status in RideCase.Status.values:
        filters["status"] = status
    if not archived and phase in RIDE_PHASE_CHOICES:
        filters["phase"] = phase
    if include_page:
        try:
            page = int(params.get("page", ""))
        except ValueError:
            pass
        else:
            if page > 1:
                filters["page"] = page
    return filters


def _ride_window_end(*, now: datetime, lead_days: int) -> date:
    return timezone.localdate(now, timezone=BERLIN) + timedelta(days=lead_days)


def ride_phase_filters(*, now: datetime, lead_days: int) -> tuple[Q, dict[str, Q]]:
    """Return shared eligibility and the two phase predicates for the proposal list and dashboard."""
    window_end = _ride_window_end(now=now, lead_days=lead_days)
    eligible = Q(direction=RideCase.ROUND_TRIP, currently_matches=True, earliest__gt=now) & ~Q(status=RideCase.Status.CLOSED)
    return eligible, {"ready": Q(travel_date__lte=window_end), "standby": Q(travel_date__gt=window_end)}


@dataclass(frozen=True)
class _Mediation:
    state: str
    label: str
    starts_on: date | None = None


def _mediation(case: RideCase, *, now: datetime, lead_days: int) -> _Mediation:
    if not case.currently_matches:
        if now >= case.earliest:
            return _Mediation("elapsed", "Terminbeginn verstrichen")
        return _Mediation("inactive", "Kein aktueller Treffer")
    if case.status == RideCase.Status.CLOSED:
        return _Mediation("closed", case.get_status_display())
    if now >= case.earliest:
        return _Mediation("started", "Lehrgang begonnen")
    if case.travel_date > _ride_window_end(now=now, lead_days=lead_days):
        return _Mediation("standby", "Standby", case.travel_date - timedelta(days=lead_days))
    return _Mediation("ready", "Bereit zur Vermittlung")


@dataclass(frozen=True)
class _InquirySummary:
    label: str
    total: int
    pending: int
    yes: int
    no: int
    unavailable: int


def _inquiry_participations():
    release_notices = RideNotice.objects.filter(
        participation_id=OuterRef("pk"), kind=RideNotice.Kind.CONTACTS,
    )
    return RideParticipation.objects.select_related("participant", "seat__participant", "seat__session").annotate(
        had_release=Exists(release_notices),
    )


def _active_inquiry_ids(case, participations, *, now):
    if not _open_test_case(case):
        return set()
    return {entry.pk for entry in participations if _has_access(entry, now)}


def _inquiry_summary(participations, active_ids):
    answers = RideParticipation.Answer
    counts = Counter(entry.answer for entry in participations)
    active = [entry for entry in participations if entry.pk in active_ids]
    pending = sum(entry.answer == answers.PENDING for entry in active)
    consents = sum(entry.answer == answers.YES and entry.consented_at is not None for entry in active)
    if not participations:
        label = "Noch nicht gestartet"
    elif consents >= 2:
        label = "Kontakte freigegeben"
    elif any(entry.had_release for entry in participations):
        label = "Kontaktfreigabe beendet"
    elif not active:
        label = "Beendet"
    elif pending:
        label = "Antworten ausstehend"
    else:
        label = "Zu wenig Zusagen"
    return _InquirySummary(
        label, len(participations), pending, counts[answers.YES], counts[answers.NO], counts[answers.PENDING] - pending,
    )


@never_cache
@require_safe
def rides(request):
    archived = request.GET.get("archiv") == "1"
    params = _list_filters(request.GET, archived=archived)
    selected_status = params.get("status", "")
    selected_phase = params.get("phase", "")
    combined = RideCase.objects.filter(direction=RideCase.ROUND_TRIP)
    counts = combined.aggregate(
        active=Count("pk", filter=Q(currently_matches=True)),
        pending=Count("pk", filter=Q(currently_matches=True) & ~Q(status=RideCase.Status.CLOSED)),
    )
    cases = RideCase.objects.exclude(direction=RideCase.ROUND_TRIP) if archived else combined
    if selected_status:
        cases = cases.filter(status=selected_status)
    now = timezone.now()
    lead_days = get_ride_lead_days() if not archived else None
    if selected_phase:
        eligible, phases = ride_phase_filters(now=now, lead_days=lead_days)
        cases = cases.filter(eligible, phases[selected_phase])
    show_inquiries = not archived and inquiry_controls_available(request)
    manual_inquiries = show_inquiries and can_edit(request.user, "rides") and not get_ride_automation()
    cases = cases.annotate(participant_count=Count("seats__participant_id", distinct=True)).order_by(
        "-currently_matches", "travel_date", "earliest", "pk",
    )
    page_obj = Paginator(cases, 25).get_page(request.GET.get("page"))
    detail_params = params | ({"page": page_obj.number} if page_obj.number > 1 else {})
    ride_cards = []
    if not archived and page_obj:
        if show_inquiries:
            prefetch_related_objects(
                [case for case in page_obj if case.is_test],
                Prefetch("participation_requests", queryset=_inquiry_participations(), to_attr="inquiry_participations"),
            )
        for case in page_obj:
            mediation = _mediation(case, now=now, lead_days=lead_days)
            can_start = mediation.state in {"ready", "standby"}
            inquiry = None
            if show_inquiries and case.is_test and (can_start or case.inquiry_participations):
                active_ids = _active_inquiry_ids(case, case.inquiry_participations, now=now)
                inquiry = _inquiry_summary(case.inquiry_participations, active_ids)
            ride_cards.append({
                "case": case,
                "mediation": mediation,
                "inquiry": inquiry,
                "can_start_inquiry": manual_inquiries and can_start and inquiry is not None and not inquiry.total,
            })
    return render(request, "workspace/rides.html", {
        "section": "rides",
        "page_obj": page_obj,
        "ride_cards": ride_cards,
        "active_count": counts["active"],
        "pending_count": counts["pending"],
        "status_choices": RideCase.Status.choices,
        "selected_status": selected_status,
        "phase_choices": RIDE_PHASE_CHOICES.items(),
        "selected_phase": selected_phase,
        "archived": archived,
        "querystring": urlencode(params),
        "detail_query": urlencode(detail_params),
        "synced": request.GET.get("abgleich") == "1",
    })


@never_cache
@require_POST
@csrf_protect
def rides_sync(request):
    sync_ride_cases()
    params = {"abgleich": "1"} | _list_filters(request.POST, include_page=True)
    return redirect(f"{reverse('rides')}?{urlencode(params)}")


def _status_form(case):
    return RideCaseStatusForm(initial={"status": case.status, "expected_status": case.status})


@sensitive_variables("rows", "previews")
def _inquiry_rows(participations, active_ids):
    rows = []
    for participation in participations:
        previews = [
            notice_preview(notice) | {"id": notice.pk, "kind": notice.kind, "label": notice.get_kind_display()}
            for notice in participation.notices.all()
        ]
        rows.append({
            "participant_name": participation.participant.name,
            "answer": participation.answer,
            "answer_label": INQUIRY_ANSWER_LABELS[participation.answer],
            "access_active": participation.pk in active_ids,
            "answered_at": participation.answered_at,
            "previews": previews,
        })
    return rows


@sensitive_variables("inquiry_rows", "participations")
@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
@csrf_protect
def ride_detail(request, case_id):
    case = get_object_or_404(RideCase, pk=case_id)
    round_trip = case.direction == RideCase.ROUND_TRIP
    list_filters = _list_filters(request.GET, archived=not round_trip, include_page=True)
    status = 200
    conflict = ""
    if request.method == "POST":
        form = RideCaseStatusForm(request.POST)
        status = 400
        if form.is_valid():
            try:
                set_ride_case_status(
                    case.pk, form.cleaned_data["status"], expected_status=form.cleaned_data["expected_status"],
                )
            except ValueError as error:
                case.refresh_from_db()
                form = _status_form(case)
                conflict = str(error)
                status = 409
            else:
                params = {"gespeichert": "1"} | list_filters
                return redirect(f"{reverse('ride_detail', args=[case.pk])}?{urlencode(params)}")
    else:
        form = _status_form(case)

    seats = case.seats.select_related("participant", "session__course").order_by("participant__name", "pk")
    show_inquiries = round_trip and case.is_test and inquiry_controls_available(request) and can_edit(request.user, "rides")
    now = timezone.now()
    participations = list(
        _inquiry_participations().filter(case=case).select_related("case", "session__course")
        .prefetch_related("notices").order_by("pk")
    ) if show_inquiries else []
    active_ids = _active_inquiry_ids(case, participations, now=now)
    inquiry_rows = _inquiry_rows(participations, active_ids)
    inquiry_result = request.GET.get("testabfrage") if show_inquiries else None
    return render(request, "workspace/ride_detail.html", {
        "section": "rides",
        "case": case,
        "round_trip": round_trip,
        "list_query": urlencode(list_filters),
        "mediation": _mediation(case, now=now, lead_days=get_ride_lead_days()) if round_trip else None,
        "seats": seats,
        "form": form,
        "conflict": conflict,
        "saved": request.method == "GET" and request.GET.get("gespeichert") == "1",
        "show_inquiries": show_inquiries,
        "inquiry_rows": inquiry_rows,
        "inquiry_summary": _inquiry_summary(participations, active_ids) if participations else None,
        "inquiry_message": INQUIRY_MESSAGES.get(inquiry_result),
        "inquiry_failed": inquiry_result == "nicht-moeglich",
    }, status=status)
