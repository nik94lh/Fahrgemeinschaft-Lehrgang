"""Queue the explicitly requested one-time replay on the existing FFP installation."""

import hashlib
import json

from django.conf import settings
from django.db import migrations


def queue_ffp_contact_replay(apps, schema_editor):
    if not settings.HTTPS or "bms.planspielplatte.de" not in settings.ALLOWED_HOSTS:
        return

    alias = schema_editor.connection.alias
    configurations = apps.get_model("mailimport", "MailboxConfiguration").objects.using(alias)
    configuration = configurations.filter(pk=1, enabled=True).exclude(encrypted_password="").first()
    if configuration is None:
        return

    identity = (
        configuration.host.lower().rstrip("."),
        configuration.port,
        configuration.username,
        configuration.mailbox,
    )
    source_id = hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode("ascii")).hexdigest()
    cursor = apps.get_model("mailimport", "MailboxCursor").objects.using(alias).filter(
        source_id=source_id, last_uid__gt=0, uid_validity__gt=0,
    ).first()
    jobs = apps.get_model("mailimport", "ContactReplayJob").objects.using(alias)
    if cursor is not None and not jobs.exists():
        jobs.create(
            status="queued",
            source_id=source_id,
            uid_validity=cursor.uid_validity,
            upper_uid=cursor.last_uid,
        )


class Migration(migrations.Migration):
    dependencies = [("mailimport", "0004_contact_replay_job")]
    operations = [migrations.RunPython(queue_ffp_contact_replay, migrations.RunPython.noop)]
