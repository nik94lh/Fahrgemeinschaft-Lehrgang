"""Operator views for the IMAP import journal."""

from datetime import date
from urllib.parse import urlencode

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST, require_safe

from accounts.access import can_edit, can_view
from bookings.models import EmailEvent, School
from bookings.participant_views import _receipt_address
from bookings.services import ACTIVATION_EVENTS, ACTIVE_STATUSES

from .contact_replay import ACTIVE_STATES, ContactReplayError, queue_receipt_contact_replay
from .imap_sync import ImapSyncError, load_configuration
from .models import ContactReplayJob, MailReceipt, MailboxCursor


CASE_STATES = (
    EmailEvent.State.NEEDS_REVIEW, EmailEvent.State.REVIEWED, EmailEvent.State.DISMISSED,
)
RECHECK_ERROR_SESSION_KEY = "mail_receipt_recheck_error"


@never_cache
@require_safe
def mail_receipts(request):
    if not settings.HTTPS or not request.is_secure():
        raise Http404

    config, mailbox_status = _active_mailbox()
    source_id = config.source_id if config else None
    cursor = MailboxCursor.objects.filter(source_id=source_id, last_sync_at__isnull=False).first() if config else None
    if config and cursor is None:
        mailbox_status = "Für das aktive Importpostfach wurde noch kein Abruf erfasst."
    archive = request.GET.get("ansicht") == "frueher"
    query = request.GET.get("q", "").strip()[:100] if not archive else ""
    if archive:
        records = EmailEvent.objects.filter(mail_receipts__isnull=True).select_related(
            "seat__participant", "seat__session__course",
        ).order_by("-imported_at", "-pk")
    else:
        records = MailReceipt.objects.order_by("-observed_at", "-pk")
        if query:
            matches = (
                Q(fields__participant_name__icontains=query)
                | Q(fields__course_name__icontains=query)
                | Q(fields__booking_reference__icontains=query)
            )
            if len(query) <= 10 and query.isascii() and query.isdecimal():
                matches |= Q(uid=int(query))
            records = records.filter(matches)
    return render(request, "mailimport/receipts.html", {
        "section": "mail_receipts",
        "archive": archive,
        "cursor": cursor,
        "source_id": source_id,
        "mailbox_status": mailbox_status,
        "query": query,
        "case_states": CASE_STATES,
        "page_obj": Paginator(records, 25).get_page(request.GET.get("page")),
        "querystring": "ansicht=frueher" if archive else urlencode({"q": query}) if query else "",
    })


def _active_mailbox():
    try:
        config = load_configuration()
    except ImapSyncError:
        return None, "Die Importkonfiguration ist unvollständig oder konnte nicht geladen werden."
    if config is None:
        return None, "Der automatische Abruf ist deaktiviert."
    return config, ""


def _get_mail_receipt(receipt_id):
    return get_object_or_404(MailReceipt.objects.select_related("event__seat__participant"), pk=receipt_id)


def _can_recheck(receipt, seat, config):
    return (
        config is not None and receipt.source_id == config.source_id
        and receipt.event is not None and receipt.event.state == EmailEvent.State.APPLIED
        and receipt.event.kind in ACTIVATION_EVENTS
        and seat is not None and seat.status in ACTIVE_STATUSES and seat.participant_id is not None
        and not seat.participant.email
    )


def _receipt_recheck_job(receipt):
    active = Q(status__in=ACTIVE_STATES, last_uid__lt=receipt.uid, upper_uid__gte=receipt.uid)
    failed = Q(status=ContactReplayJob.Status.FAILED, last_uid=receipt.uid - 1, upper_uid=receipt.uid)
    if receipt.address_checked_at:
        failed &= Q(created_at__gt=receipt.address_checked_at)
    return ContactReplayJob.objects.filter(
        active | failed, source_id=receipt.source_id, uid_validity=receipt.uid_validity,
    ).order_by("-created_at", "-pk").first()


@never_cache
@require_POST
@csrf_protect
def mail_receipt_recheck(request, receipt_id):
    if not settings.HTTPS or not request.is_secure():
        raise Http404

    receipt = _get_mail_receipt(receipt_id)
    try:
        queue_receipt_contact_replay(receipt)
    except ContactReplayError as error:
        request.session[RECHECK_ERROR_SESSION_KEY] = {"receipt_id": receipt_id, "message": str(error)}
    return redirect("mail_receipt", receipt_id=receipt_id)


@never_cache
@require_safe
def mail_receipt(request, receipt_id):
    if not settings.HTTPS or not request.is_secure():
        raise Http404

    receipt = _get_mail_receipt(receipt_id)
    seat = receipt.event.seat if receipt.event_id else None
    config, _ = _active_mailbox()
    can_recheck = _can_recheck(receipt, seat, config)
    recheck_error = request.session.get(RECHECK_ERROR_SESSION_KEY, {})
    if recheck_error.get("receipt_id") == receipt_id:
        request.session.pop(RECHECK_ERROR_SESSION_KEY)
    else:
        recheck_error = {}
    fields = receipt.fields.copy()
    for field in ("starts_at", "ends_at"):
        if value := fields.get(field):
            fields[field] = parse_datetime(value)
    for field in ("confirmation_deadline", "cancellation_date"):
        if value := fields.get(field):
            fields[field] = date.fromisoformat(value)

    show_booking = bool(
        can_view(request.user, "bookings") and seat
        and receipt.event.state in (EmailEvent.State.APPLIED, EmailEvent.State.REVIEWED)
    )
    show_profile = show_booking and bool(seat.participant_id)
    current_profile_address = seat.participant.email if show_profile else ""
    address_handoff = False
    address_handoff_issue = ""
    if (
        show_profile and not current_profile_address and can_edit(request.user, "bookings")
        and receipt.event.state == EmailEvent.State.APPLIED
        and fields.get("participant_email_candidate") and not fields.get("participant_email")
    ):
        suggestion, issue = _receipt_address(seat, str(receipt.pk))
        address_handoff = suggestion is not None
        if not address_handoff:
            address_handoff_issue = issue or "unavailable"

    return render(request, "mailimport/receipt_detail.html", {
        "section": "mail_receipts",
        "receipt": receipt,
        "fields": fields,
        "school_name": dict(School.choices).get(fields.get("school")),
        "has_review": receipt.event and receipt.event.state in CASE_STATES,
        "show_booking": show_booking,
        "show_profile": show_profile,
        "current_profile_address": current_profile_address,
        "address_handoff": address_handoff,
        "address_handoff_issue": address_handoff_issue,
        "mailbox": config.mailbox if config and config.source_id == receipt.source_id else "",
        "can_recheck": can_recheck,
        "recheck_job": _receipt_recheck_job(receipt) if can_recheck else None,
        "recheck_error": recheck_error.get("message"),
    })
