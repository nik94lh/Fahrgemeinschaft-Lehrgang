from django.db import models

from .access import SECTIONS, section_actions


PERMISSION_LABELS = {"view": "ansehen", "edit": "bearbeiten"}


class WorkspaceAccess(models.Model):
    """Content type for access rights managed by Django's authorization backend."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = tuple(
            (f"{action}_{section}", f"{name} {PERMISSION_LABELS[action]}")
            for section, name in SECTIONS.items()
            for action in section_actions(section)
        )


class LoginAttemptBucket(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    window_start = models.DateTimeField(db_index=True)
    attempts = models.PositiveSmallIntegerField()

    class Meta:
        default_permissions = ()
