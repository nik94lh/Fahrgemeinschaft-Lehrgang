from django.db import migrations, models

from bookings.ride_codes import code_digest, compact_code_v1, legacy_code


def add_short_aliases(apps, schema_editor):
    participation = apps.get_model("bookings", "RideParticipation")
    entries = participation.objects.using(schema_editor.connection.alias)
    batch = []
    for entry in entries.only("token_seed", "token_digest").iterator(chunk_size=500):
        if entry.token_digest != code_digest(legacy_code(entry.token_seed)):
            continue
        entry.short_token_digest = code_digest(compact_code_v1(entry.token_seed))
        batch.append(entry)
        if len(batch) == 500:
            entries.bulk_update(batch, ["short_token_digest"])
            batch.clear()
    if batch:
        entries.bulk_update(batch, ["short_token_digest"])


def restore_supported_notices(apps, schema_editor):
    notice = apps.get_model("bookings", "RideNotice")
    notice.objects.using(schema_editor.connection.alias).filter(kind="new_peer").update(kind="contacts")


class Migration(migrations.Migration):
    dependencies = [("bookings", "0018_previous_booking_data")]

    operations = [
        migrations.AddField(
            model_name="rideparticipation",
            name="short_token_digest",
            field=models.CharField(editable=False, max_length=64, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name="ridenotice",
            name="kind",
            field=models.CharField(max_length=10, choices=[
                ("invitation", "Einladung zur Fahrgemeinschaft"),
                ("contacts", "Kontakte zur Fahrgemeinschaft verfügbar"),
                ("new_peer", "Weitere Zusage zur Fahrgemeinschaft"),
                ("withdrawal", "Zusage zur Fahrgemeinschaft zurückgezogen"),
            ]),
        ),
        migrations.RunPython(add_short_aliases, restore_supported_notices),
    ]
