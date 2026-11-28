from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("mailimport", "0008_mail_receipt_address_check")]

    operations = [
        migrations.RemoveConstraint(model_name="mailboxconfiguration", name="single_mailbox_configuration"),
        migrations.AddConstraint(
            model_name="mailboxconfiguration",
            constraint=models.CheckConstraint(condition=models.Q(id__in=(1, 2)), name="mailimport_two_mailbox_slots"),
        ),
        migrations.AddConstraint(
            model_name="mailboxconfiguration",
            constraint=models.UniqueConstraint(
                fields=("host", "port", "username", "mailbox"), name="mailimport_unique_mailbox",
            ),
        ),
        migrations.AddField(
            model_name="mailreceipt", name="mailbox_slot",
            field=models.PositiveSmallIntegerField(
                db_default=1, choices=[(1, "IMAP-Eingang A"), (2, "IMAP-Eingang B")],
            ),
        ),
    ]
