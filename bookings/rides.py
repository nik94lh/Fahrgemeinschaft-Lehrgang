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
from mailimport import outbound
from mailimport.participant_settings import get_functional_addresses

from .models import RideCase, RideNotice, RideParticipation
from .ride_history import HISTORY_MODES
from .ride_inquiry_views import get_ride_automation, inquiry_controls_available
from .ride_mail import can_resend_ride_notice
from .ride_participation import _has_access, _open_ride_case, notice_preview, uninvited_participants, with_case_membership
from .ride_settings import get_ride_lead_days
from .rides_matching import BERLIN, set_ride_case_status, sync_ride_cases


RIDE_PHASE_CHOICES = {"ready": "Bereit zur Vermittlung", "standby": "Standby"}
INQUIRY_MESSAGES = {
    "vorbereitet": "Testabfrage vorbereitet. Die Einladungen stehen als Vorschau bereit.",
    "unveraendert": "Für diesen Vorschlag sind bereits Testeinladungen vorbereitet.",
    "nicht-moeglich": "Die Testabfrage lässt sich für diesen Vorschlag derzeit nicht starten.",
}
LIVE_INQUIRY_MESSAGES = {
    "vorbereitet": "Die Einladungen sind für den Versand vorgemerkt.",
    "unveraendert": "Für die passenden Personen gibt es bereits Einladungen.",
    "nicht-moeglich": "Für den Start müssen die Automatik aus, das Versandkonto aktiv und mindestens zwei passende persönliche Adressen vorhanden sein.",
    "erneut-vorgemerkt": "Die Nachricht ist für den erneuten Versand vorgemerkt.",
    "erneut-nicht-moeglich": "Die Nachricht kann derzeit nicht erneut gesendet werden.",
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
    eligible = Q(
        direction=RideCase.ROUND_TRIP, currently_matches=True, removed_at__isnull=True, earliest__gt=now,
    ) & ~Q(status=RideCase.Status.CLOSED)
    return eligible, {"ready": Q(travel_date__lte=window_end), "standby": Q(travel_date__gt=window_end)}


@dataclass(frozen=True)
class _Mediation:
    state: str
    label: str
    starts_on: date | None = None


def _mediation(case: RideCase, *, now: datetime, lead_days: int) -> _Mediation:
    if case.removed_at is not None:
        return _Mediation("removed", "Manuell entfernt")
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
    state: str
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
    participations = RideParticipation.objects.select_related("participant", "seat__participant", "seat__session")
    return with_case_membership(participations).annotate(
        had_release=Exists(release_notices),
    )


def _active_inquiry_ids(case, participations, *, now):
    if not participations or not _open_ride_case(case):
        return set()
    excluded = () if case.is_test else get_functional_addresses()
    return {entry.pk for entry in participations if _has_access(entry, now, functional_addresses=excluded)}


def _inquiry_summary(participations, active_ids):
    answers = RideParticipation.Answer
    counts = Counter(entry.answer for entry in participations)
    active = [entry for entry in participations if entry.pk in active_ids]
    pending = sum(entry.answer == answers.PENDING for entry in active)
    consents = sum(entry.answer == answers.YES and entry.consented_at is not None for entry in active)
    released_before = any(entry.had_release for entry in participations)
    if not participations:
        state, label = "not-started", "Noch nicht gestartet"
    elif consents >= 2:
        state, label = "contacts-released", "Kontakte freigegeben"
    elif pending:
        state = "awaiting-replies"
        label = "Kontaktfreigabe beendet, weitere Antworten offen" if released_before else "Antworten ausstehend"
    else:
        state = "no-match"
        label = "Kontaktfreigabe beendet" if released_before else ("Zu wenig Zusagen" if active else "Beendet")
    return _InquirySummary(
        state=state, label=label, total=len(participations), pending=pending, yes=counts[answers.YES],
        no=counts[answers.NO], unavailable=counts[answers.PENDING] - pending,
    )


@never_cache
@require_safe
def rides(request):
    archived = request.GET.get("archiv") == "1"
    params = _list_filters(request.GET, archived=archived)
    selected_status = params.get("status", "")
    selected_phase = params.get("phase", "")
    if not archived and selected_status == RideCase.Status.CLOSED:
        return redirect("ride_history")
    now = timezone.now()
    lead_days = get_ride_lead_days() if not archived else None
    active_count = 0
    if archived:
        cases = RideCase.objects.exclude(direction=RideCase.ROUND_TRIP)
    else:
        eligible, phases = ride_phase_filters(now=now, lead_days=lead_days)
        cases = RideCase.objects.filter(eligible)
        active_count = cases.count()
        if selected_phase:
            cases = cases.filter(phases[selected_phase])
    if selected_status:
        cases = cases.filter(status=selected_status)
    show_inquiries = not archived and inquiry_controls_available(request)
    manual_inquiries = show_inquiries and can_edit(request.user, "rides") and not get_ride_automation()
    live_manual = manual_inquiries and outbound.outbound_enabled()
    if manual_inquiries:
        cases = cases.annotate(has_uninvited=uninvited_participants())
    cases = cases.annotate(participant_count=Count("seats__participant_id", distinct=True)).order_by(
        "-currently_matches", "travel_date", "earliest", "pk",
    )
    page_obj = Paginator(cases, 25).get_page(request.GET.get("page"))
    detail_params = params | ({"page": page_obj.number} if page_obj.number > 1 else {})
    ride_cards = []
    if not archived and page_obj:
        if show_inquiries:
            prefetch_related_objects(
                list(page_obj),
                Prefetch("participation_requests", queryset=_inquiry_participations(), to_attr="inquiry_participations"),
            )
        for case in page_obj:
            mediation = _mediation(case, now=now, lead_days=lead_days)
            can_start = mediation.state in {"ready", "standby"}
            inquiry = None
            can_launch = can_start and (case.is_test or live_manual)
            if show_inquiries and (can_launch or case.inquiry_participations):
                active_ids = _active_inquiry_ids(case, case.inquiry_participations, now=now)
                inquiry = _inquiry_summary(case.inquiry_participations, active_ids)
            ride_cards.append({
                "case": case,
                "mediation": mediation,
                "inquiry": inquiry,
                "can_start_inquiry": manual_inquiries and can_launch and case.has_uninvited,
            })
    return render(request, "workspace/rides.html", {
        "section": "rides",
        "page_obj": page_obj,
        "ride_cards": ride_cards,
        "active_count": active_count,
        "status_choices": [choice for choice in RideCase.Status.choices if archived or choice[0] != RideCase.Status.CLOSED],
        "selected_status": selected_status,
        "phase_choices": RIDE_PHASE_CHOICES.items(),
        "selected_phase": selected_phase,
        "archived": archived,
        "querystring": urlencode(params),
        "detail_query": urlencode(detail_params),
        "synced": request.GET.get("abgleich") == "1",
        "removed": request.GET.get("entfernt") == "1",
        "restored": request.GET.get("wiederhergestellt") == "1",
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
def _inquiry_rows(participations, active_ids, *, is_test, now):
    rows = []
    for participation in participations:
        previews, messages = [], []
        for notice in participation.notices.all():
            message = {"id": notice.pk, "kind": notice.kind, "label": notice.get_kind_display()}
            if is_test:
                previews.append(message | notice_preview(notice))
            else:
                delivery = next(iter(notice.deliveries.all()), None)
                messages.append(message | {
                    "delivery": delivery,
                    "can_resend": participation.pk in active_ids and can_resend_ride_notice(notice, now=now),
                })
        rows.append({
            "participant_name": participation.participant.name,
            "answer": participation.answer,
            "answer_label": "Antwort offen" if not is_test and participation.answer == RideParticipation.Answer.PENDING
            else INQUIRY_ANSWER_LABELS[participation.answer],
            "access_active": participation.pk in active_ids,
            "answered_at": participation.answered_at,
            "previews": previews,
            "messages": messages,
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
    history_mode = request.GET.get("verlauf", "")
    if round_trip and case.removed_at is not None:
        history_mode = "entfernt"
    history_url = ""
    if round_trip and history_mode in HISTORY_MODES:
        list_filters = {"verlauf": history_mode} | ({"page": list_filters["page"]} if "page" in list_filters else {})
        history_url = reverse("ride_history", query=list_filters)
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
    show_inquiries = round_trip and inquiry_controls_available(request) and can_edit(request.user, "rides")
    now = timezone.now()
    participations = []
    if show_inquiries:
        related_notices = "notices" if case.is_test else "notices__deliveries"
        participations = list(_inquiry_participations().filter(case=case).select_related("case", "session__course")
                              .prefetch_related(related_notices).order_by("pk"))
    active_ids = _active_inquiry_ids(case, participations, now=now)
    inquiry_rows = _inquiry_rows(participations, active_ids, is_test=case.is_test, now=now)
    inquiry_result = request.GET.get("testabfrage" if case.is_test else "abfrage") if show_inquiries else None
    inquiry_messages = INQUIRY_MESSAGES if case.is_test else LIVE_INQUIRY_MESSAGES
    return render(request, "workspace/ride_detail.html", {
        "section": "rides",
        "case": case,
        "round_trip": round_trip,
        "list_query": urlencode(list_filters),
        "history_url": history_url,
        "history_mode": history_mode if history_url else "",
        "history_page": list_filters.get("page", "") if history_url else "",
        "mediation": _mediation(case, now=now, lead_days=get_ride_lead_days()) if round_trip else None,
        "seats": seats,
        "form": form,
        "conflict": conflict,
        "saved": request.method == "GET" and request.GET.get("gespeichert") == "1",
        "show_inquiries": show_inquiries and (case.is_test or bool(participations) or outbound.outbound_enabled()),
        "inquiry_rows": inquiry_rows,
        "inquiry_summary": _inquiry_summary(participations, active_ids) if participations else None,
        "inquiry_message": inquiry_messages.get(inquiry_result),
        "inquiry_failed": inquiry_result in {"nicht-moeglich", "erneut-nicht-moeglich"},
        "can_confirm_ride": round_trip and case.removed_at is None and case.return_latest <= now,
        "action_failed": request.GET.get("aktion") == "nicht-moeglich",
        "restored": request.GET.get("wiederhergestellt") == "1",
        "outcome_saved": request.GET.get("fahrt") in {"0", "1"},
    }, status=status)
