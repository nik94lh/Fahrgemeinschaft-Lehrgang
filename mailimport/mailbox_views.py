from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from bookings.contact_replay import ContactReplayResult

from .contact_replay import ContactReplayError, queue_contact_replay
from .imap_sync import ImapSyncError, load_configuration, load_second_configuration, mailbox_source_id
from .mailbox_forms import FunctionalAddressesForm, MailboxForm
from .models import ContactReplayJob, MailboxConfiguration, MailboxCursor, ParticipantRecognitionConfiguration
from .participant_settings import get_functional_addresses


REPLAY_SKIP_LABELS = {
    ContactReplayResult.ADDRESS_REVIEW: "Adresse zur Bestätigung im Mail-Eingang",
    ContactReplayResult.NO_ADDRESS: "Keine eindeutig passende Teilnehmeradresse erkannt",
    ContactReplayResult.AMBIGUOUS_ADDRESS: "Mehrere mögliche Teilnehmeradressen",
    ContactReplayResult.SAME_ADDRESS: "Adresse bereits hinterlegt",
    ContactReplayResult.OTHER_ADDRESS: "Andere Adresse im Teilnehmerprofil hinterlegt",
    ContactReplayResult.ADDRESS_IN_USE: "Adresse gehört zu einem anderen Profil",
    ContactReplayResult.SHARED_PROFILE: "Leeres Teilnehmerprofil gehört zu mehreren Buchungen",
    ContactReplayResult.INVALID: "Buchungsangaben fehlen oder sind widersprüchlich",
    ContactReplayResult.NO_APPLIED_EVENT: "Kein bereits angewendetes Buchungsereignis",
    ContactReplayResult.NO_ACTIVE_BOOKING: "Keine aktive Buchung mit zugeordnetem Teilnehmer",
    ContactReplayResult.BOOKING_CHANGED: "Buchungsangaben passen nicht zum heutigen Stand",
    ContactReplayResult.PARTICIPANT_CHANGED: "Ursprünglicher Teilnehmer nicht mehr zweifelsfrei zugeordnet",
    ContactReplayResult.OPEN_REVIEW: "Offener Klärungsfall zur Buchung",
    ContactReplayResult.PROFILE_CHANGED: "Profil während des Abgleichs geändert",
    ContactReplayResult.UNSUPPORTED: "Keine Genehmigung oder Teilnahmebestätigung",
    ContactReplayResult.UNREADABLE: "Mail nicht verfügbar, zu groß oder nicht lesbar",
}

INPUT_SLOTS = MailboxConfiguration.Slot
DUPLICATE_MAILBOX = "Diese Postfachverbindung ist bereits beim anderen IMAP-Eingang eingerichtet."


def _source_id(mailbox):
    return mailbox_source_id(mailbox.host, mailbox.port, mailbox.username, mailbox.mailbox)


def _input_status(slot, record):
    load = load_configuration if slot == INPUT_SLOTS.FIRST else load_second_configuration
    config_error = False
    try:
        config = load()
    except ImapSyncError:
        config = None
        config_error = True
    return {
        "configured": record is not None,
        "server_configured": record is None and config is not None,
        "configuration_error": config_error,
        "enabled": config is not None or (record is not None and record.enabled),
        "password_saved": bool(record and record.encrypted_password),
        "source_id": _source_id(record) if record else config.source_id if config else None,
        "live_region": "mailbox-status" if slot == INPUT_SLOTS.FIRST else "mailbox-status-second",
    }


@sensitive_variables()
def _save_mailbox_form(data, slot):
    with transaction.atomic():
        records = {record.pk: record for record in MailboxConfiguration.objects.select_for_update().order_by("pk")}
        form = _mailbox_form(slot, records.get(slot), data=data)
        if not form.is_valid():
            return form
        values = {
            field: form.cleaned_data[field]
            for field in ("host", "port", "username", "mailbox", "encrypted_password", "enabled")
        }
        source_id = mailbox_source_id(values["host"], values["port"], values["username"], values["mailbox"])
        other_slot = INPUT_SLOTS.SECOND if slot == INPUT_SLOTS.FIRST else INPUT_SLOTS.FIRST
        other = _input_status(other_slot, records.get(other_slot))
        if source_id == other["source_id"]:
            form.add_error(None, DUPLICATE_MAILBOX)
            return form
        try:
            MailboxConfiguration.objects.update_or_create(pk=slot, defaults=values)
        except IntegrityError:
            form.add_error(None, DUPLICATE_MAILBOX)
            return form
    return None


def _mailbox_form(slot, record, **kwargs):
    auto_id = "id_%s" if slot == INPUT_SLOTS.FIRST else "id_imap_b_%s"
    return MailboxForm(configuration=record, auto_id=auto_id, **kwargs)


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
    slot = INPUT_SLOTS.FIRST
    if is_post and not saving_addresses:
        try:
            slot = INPUT_SLOTS(int(request.POST.get("slot", INPUT_SLOTS.FIRST)))
        except ValueError:
            raise Http404 from None

    bound_form = None
    replay_error = ""
    if action == "start-contact-replay":
        try:
            if slot == INPUT_SLOTS.FIRST:
                queue_contact_replay()
            else:
                queue_contact_replay(slot)
        except ContactReplayError as error:
            replay_error = str(error)
        else:
            return redirect("mailbox")
    elif is_post and not saving_addresses:
        bound_form = _save_mailbox_form(request.POST, slot)
        if bound_form is None:
            return redirect(f"{reverse('mailbox')}?gespeichert={slot.value}")

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

    records = MailboxConfiguration.objects.in_bulk()
    inputs = []
    saved = "" if is_post else request.GET.get("gespeichert")
    for input_slot in INPUT_SLOTS:
        record = records.get(input_slot)
        inputs.append({
            **_input_status(input_slot, record),
            "slot": input_slot,
            "form": bound_form if bound_form is not None and slot == input_slot else _mailbox_form(input_slot, record),
            "saved": saved == str(input_slot.value),
        })
    if inputs[0]["source_id"] and inputs[0]["source_id"] == inputs[1]["source_id"]:
        inputs[1]["duplicate"] = True
    cursors = MailboxCursor.objects.in_bulk([source["source_id"] for source in inputs if source["source_id"]])
    for source in inputs:
        source["cursor"] = cursors.get(source["source_id"])

    replay_job = ContactReplayJob.objects.order_by("-created_at", "-pk").first()
    active_replay_statuses = (ContactReplayJob.Status.QUEUED, ContactReplayJob.Status.RUNNING)
    counts = replay_job.outcome_counts if replay_job else {}
    return render(request, "mailimport/mailbox.html", {
        "inputs": inputs,
        "addresses_form": addresses_form,
        "addresses_saved": not is_post and request.GET.get("funktionsadressen_gespeichert") == "1",
        "section": "mailbox",
        "replay_job": replay_job,
        "replay_source_label": next((source["slot"].label for source in inputs if replay_job and source["source_id"] == replay_job.source_id), "Früheres Postfach"),
        "replay_error": replay_error,
        "replay_active": replay_job is not None and replay_job.status in active_replay_statuses,
        "replay_skip_counts": [
            (label, counts[outcome.name]) for outcome, label in REPLAY_SKIP_LABELS.items()
            if counts.get(outcome.name)
        ],
        "replay_unclassified_count": max(0, replay_job.scanned_count - sum(counts.values())) if replay_job else 0,
    }, status=400 if replay_error else 200)
