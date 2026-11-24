from django.conf import settings
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from .contact_replay import ContactReplayError, queue_contact_replay
from .imap_sync import mailbox_source_id
from .mailbox_forms import FunctionalAddressesForm, MailboxForm
from .models import ContactReplayJob, MailboxConfiguration, MailboxCursor, ParticipantRecognitionConfiguration
from .participant_settings import get_functional_addresses


@sensitive_post_parameters("password", "functional_addresses")
@sensitive_variables()
@never_cache
@require_http_methods(["GET", "POST"])
@csrf_protect
def mailbox_view(request):
    if not settings.HTTPS or not request.is_secure():
        raise Http404

    is_post = request.method == "POST"
    action = request.POST.get("action", "") if is_post else ""
    saving_addresses = action == "save-functional-addresses"
    starting_replay = action == "start-contact-replay"
    replay_error = ""
    if starting_replay:
        try:
            queue_contact_replay()
        except ContactReplayError as error:
            replay_error = str(error)
        else:
            return redirect("mailbox")

    if is_post and not (saving_addresses or starting_replay):
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

    if saving_addresses:
        addresses_form = FunctionalAddressesForm(request.POST)
        if addresses_form.is_valid():
            ParticipantRecognitionConfiguration.objects.update_or_create(
                pk=1, defaults={"functional_addresses": addresses_form.cleaned_data["functional_addresses"]},
            )
            return redirect(f"{reverse('mailbox')}?funktionsadressen_gespeichert=1")
    else:
        addresses_form = FunctionalAddressesForm(
            initial={"functional_addresses": "\n".join(get_functional_addresses())},
        )

    last_sync_at = None
    last_sync_ok = None
    if record is not None:
        source_id = mailbox_source_id(record.host, record.port, record.username, record.mailbox)
        status = MailboxCursor.objects.filter(source_id=source_id).values_list(
            "last_sync_at", "last_sync_ok",
        ).first()
        if status is not None:
            last_sync_at, last_sync_ok = status

    replay_job = ContactReplayJob.objects.order_by("-created_at", "-pk").first()
    active_replay_statuses = (ContactReplayJob.Status.QUEUED, ContactReplayJob.Status.RUNNING)
    return render(request, "mailimport/mailbox.html", {
        "form": form,
        "addresses_form": addresses_form,
        "addresses_saved": not is_post and request.GET.get("funktionsadressen_gespeichert") == "1",
        "section": "mailbox",
        "configured": record is not None,
        "enabled": record is not None and record.enabled,
        "password_saved": bool(record and record.encrypted_password),
        "last_sync_at": last_sync_at,
        "last_sync_ok": last_sync_ok,
        "saved": request.method == "GET" and request.GET.get("gespeichert") == "1",
        "replay_job": replay_job,
        "replay_error": replay_error,
        "replay_active": replay_job is not None and replay_job.status in active_replay_statuses,
    }, status=400 if replay_error else 200)
