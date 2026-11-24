from django.db import models


class MailboxConfiguration(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    host = models.CharField(max_length=253)
    port = models.PositiveSmallIntegerField(default=993)
    username = models.CharField(max_length=255)
    mailbox = models.CharField(max_length=255, default="INBOX")
    encrypted_password = models.TextField(blank=True)
    enabled = models.BooleanField(default=False)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="single_mailbox_configuration")]


class MailboxCursor(models.Model):
    source_id = models.CharField(max_length=64, primary_key=True, editable=False)
    uid_validity = models.PositiveBigIntegerField(default=0)
    last_uid = models.PositiveBigIntegerField(default=0)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_sync_ok = models.BooleanField(null=True, blank=True)


class ParticipantRecognitionConfiguration(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    functional_addresses = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="single_participant_recognition_configuration")
        ]


class ContactReplayJob(models.Model):
    """Progress of an explicit one-time reread of previously imported mail."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Vorgemerkt"
        RUNNING = "running", "Läuft"
        COMPLETED = "completed", "Abgeschlossen"
        FAILED = "failed", "Unterbrochen"

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    source_id = models.CharField(max_length=64)
    uid_validity = models.PositiveBigIntegerField()
    upper_uid = models.PositiveBigIntegerField()
    last_uid = models.PositiveBigIntegerField(default=0)
    scanned_count = models.PositiveBigIntegerField(default=0)
    enriched_count = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                models.Value(1),
                condition=models.Q(status__in=["queued", "running"]),
                name="mailimport_one_active_contact_replay",
            ),
        ]
