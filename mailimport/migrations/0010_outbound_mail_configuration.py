from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("mailimport", "0009_two_imap_inputs")]

    operations = [
        migrations.CreateModel(
            name="OutboundMailConfiguration",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("sender_source", models.CharField(choices=[
                    ("separate", "Separates Versandkonto"), ("input1", "Zugangsdaten von Eingang 1"),
                    ("input2", "Zugangsdaten von Eingang 2"),
                ], default="separate", max_length=8)),
                ("host", models.CharField(max_length=253)),
                ("port", models.PositiveSmallIntegerField(default=587)),
                ("security", models.CharField(choices=[("starttls", "STARTTLS"), ("tls", "Direktes TLS")], default="starttls", max_length=8)),
                ("sender_address", models.EmailField(max_length=254)),
                ("username", models.CharField(blank=True, max_length=255)),
                ("encrypted_password", models.TextField(blank=True)),
                ("linked_identity", models.CharField(blank=True, max_length=64)),
                ("enabled", models.BooleanField(default=False)),
                ("allow_automatic_inquiries", models.BooleanField(default=False)),
            ],
            options={"constraints": [models.CheckConstraint(
                condition=models.Q(id=1), name="single_outbound_mail_configuration",
            )]},
        ),
    ]
