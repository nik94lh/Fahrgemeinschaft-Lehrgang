"""Recheck previously seen IMAP messages for participant addresses on request."""

import imaplib

from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone

from bookings.contact_replay import enrich_contact_from_imported_mail

from .imap_sync import (
    MAX_UID,
    SEARCH_WINDOW,
    ImapSyncError,
    _connect_readonly,
    _read_uid,
    _response_number,
    _search,
    load_configuration,
)
from .models import ContactReplayJob, MailboxCursor


BATCH_SIZE = 25
ACTIVE_STATES = (ContactReplayJob.Status.QUEUED, ContactReplayJob.Status.RUNNING)


class ContactReplayError(ValueError):
    """A contact replay could not be started or continued safely."""


def _active_jobs():
    return ContactReplayJob.objects.filter(status__in=ACTIVE_STATES).order_by("pk")


def queue_contact_replay() -> ContactReplayJob:
    """Pin the current source and the already imported UID range."""
    if active := _active_jobs().first():
        return active
    try:
        config = load_configuration()
    except ImapSyncError:
        raise ContactReplayError("Die IMAP-Einstellungen für den Kontaktabgleich sind nicht verfügbar.") from None
    if config is None:
        raise ContactReplayError("Aktiviere zuerst den Abruf des freigegebenen Postfachs.")
    cursor = MailboxCursor.objects.filter(source_id=config.source_id).first()
    if cursor is None or not (1 <= cursor.uid_validity <= MAX_UID and 1 <= cursor.last_uid <= MAX_UID):
        raise ContactReplayError("Warte zuerst auf einen erfolgreichen Import aus diesem Postfach.")

    try:
        with transaction.atomic():
            return ContactReplayJob.objects.create(
                source_id=config.source_id,
                uid_validity=cursor.uid_validity,
                upper_uid=cursor.last_uid,
            )
    except IntegrityError:
        # A concurrent request may have created the single active job first.
        if active := _active_jobs().first():
            return active
        raise ContactReplayError("Der Kontaktabgleich konnte gerade nicht vorgemerkt werden.") from None


def _scan_step(job: ContactReplayJob) -> tuple[int, int, int]:
    config = load_configuration()
    if config is None or config.source_id != job.source_id:
        raise ContactReplayError("Das ausgewählte Postfach wurde geändert oder der Abruf wurde deaktiviert.")
    if job.last_uid > job.upper_uid:
        raise ContactReplayError("Der gespeicherte UID-Bereich des Auftrags ist ungültig.")
    if job.last_uid == job.upper_uid:
        return job.last_uid, 0, 0

    with _connect_readonly(config) as client:
        if _response_number(client, "UIDVALIDITY") != job.uid_validity:
            raise ContactReplayError("Das Postfach hat seine UID-Gültigkeit geändert. Starte den Abgleich nach einem neuen Import.")
        if _response_number(client, "UIDNEXT") - 1 < job.upper_uid:
            raise ContactReplayError("Das Postfach meldet einen abweichenden UID-Bereich.")

        window_end = min(job.upper_uid, job.last_uid + SEARCH_WINDOW)
        uids = _search(client, job.last_uid + 1, window_end)
        batch = uids[:BATCH_SIZE]
        messages = [_read_uid(client, uid) for uid in batch]

    # The bounded batch is read before taking the booking import lock.
    enriched = sum(raw is not None and enrich_contact_from_imported_mail(raw) for raw in messages)
    end_uid = batch[-1] if len(uids) > BATCH_SIZE else window_end
    return end_uid, len(batch), enriched


@transaction.atomic
def process_contact_replay() -> ContactReplayJob | None:
    """Advance the explicit job by one bounded batch."""
    job = _active_jobs().select_for_update().first()
    if job is None:
        return None

    job.status = ContactReplayJob.Status.RUNNING
    job.error_message = ""
    try:
        # If a scan fails halfway, revert profile enrichment as well as UID progress.
        with transaction.atomic():
            last_uid, scanned, enriched = _scan_step(job)
    except ContactReplayError as error:
        job.status = ContactReplayJob.Status.FAILED
        job.error_message = str(error)
    except (ImapSyncError, OSError, imaplib.IMAP4.error, DatabaseError):
        job.error_message = "Der Kontaktabgleich wurde unterbrochen und wird beim nächsten Abruf fortgesetzt."
    except Exception:
        job.status = ContactReplayJob.Status.FAILED
        job.error_message = "Der Kontaktabgleich konnte wegen eines unerwarteten Fehlers nicht abgeschlossen werden."
    else:
        job.last_uid = last_uid
        job.scanned_count += scanned
        job.enriched_count += enriched
        if last_uid == job.upper_uid:
            job.status = ContactReplayJob.Status.COMPLETED

    if job.status in {ContactReplayJob.Status.COMPLETED, ContactReplayJob.Status.FAILED}:
        job.finished_at = timezone.now()
    job.save(update_fields=(
        "status", "last_uid", "scanned_count", "enriched_count", "error_message", "finished_at", "updated_at",
    ))
    return job
