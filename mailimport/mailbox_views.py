from django.conf import settings
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from .imap_sync import mailbox_source_id
from .mailbox_forms import MailboxForm
from .models import MailboxConfiguration, MailboxCursor


@sensitive_post_parameters("password")
@sensitive_variables()
@never_cache
@require_http_methods(["GET", "POST"])
@csrf_protect
def mailbox_view(request):
    if not settings.HTTPS or not request.is_secure():
        raise Http404

    if request.method == "POST":
        with transaction.atomic():
            record = MailboxConfiguration.objects.select_for_update().first()
            form = MailboxForm(request.POST, configuration=record)
            if form.is_valid():
                values = {
                    field: form.cleaned_data[field]
                    for field in ("host", "port", "username", "mailbox", "encrypted_password", "enabled")
                }
                MailboxConfiguration.objects.update_or_create(pk=1, defaults=values)
                return redirect(f"{reverse('mailbox')}?gespeichert=1")
    else:
        record = MailboxConfiguration.objects.first()
        form = MailboxForm(configuration=record)

    last_sync_at = None
    last_sync_ok = None
    if record is not None:
        source_id = mailbox_source_id(record.host, record.port, record.username, record.mailbox)
        status = MailboxCursor.objects.filter(source_id=source_id).values_list(
            "last_sync_at", "last_sync_ok",
        ).first()
        if status is not None:
            last_sync_at, last_sync_ok = status

    return render(request, "mailimport/mailbox.html", {
        "form": form,
        "section": "mailbox",
        "configured": record is not None,
        "enabled": record is not None and record.enabled,
        "password_saved": bool(record and record.encrypted_password),
        "last_sync_at": last_sync_at,
        "last_sync_ok": last_sync_ok,
        "saved": request.method == "GET" and request.GET.get("gespeichert") == "1",
    })
