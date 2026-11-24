"""Operator views for the IMAP import journal."""

from datetime import date
from urllib.parse import urlencode

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from bookings.models import EmailEvent, School

from .imap_sync import ImapSyncError, load_configuration
from .models import MailReceipt, MailboxCursor


CASE_STATES = (
    EmailEvent.State.NEEDS_REVIEW, EmailEvent.State.REVIEWED, EmailEvent.State.DISMISSED,
)


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


@never_cache
@require_safe
def mail_receipt(request, receipt_id):
    if not settings.HTTPS or not request.is_secure():
        raise Http404

    receipt = get_object_or_404(MailReceipt.objects.select_related("event__seat__participant"), pk=receipt_id)
    seat = receipt.event.seat if receipt.event_id else None
    config, _ = _active_mailbox()
    fields = receipt.fields.copy()
    for field in ("starts_at", "ends_at"):
        if value := fields.get(field):
            fields[field] = parse_datetime(value)
    for field in ("confirmation_deadline", "cancellation_date"):
        if value := fields.get(field):
            fields[field] = date.fromisoformat(value)

    return render(request, "mailimport/receipt_detail.html", {
        "section": "mail_receipts",
        "receipt": receipt,
        "fields": fields,
        "school_name": dict(School.choices).get(fields.get("school")),
        "has_review": receipt.event and receipt.event.state in CASE_STATES,
        "has_current_address": bool(seat and seat.participant_id and seat.participant.email),
        "mailbox": config.mailbox if config and config.source_id == receipt.source_id else "",
    })
