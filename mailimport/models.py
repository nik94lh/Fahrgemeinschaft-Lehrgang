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
