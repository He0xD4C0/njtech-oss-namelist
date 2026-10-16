from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0003_pendingavatardeletion")]

    operations = [
        migrations.AddField("syncrun", "execution_token", models.UUIDField(null=True, blank=True, editable=False)),
        migrations.AddField("syncrun", "lease_expires_at", models.DateTimeField(null=True, blank=True, editable=False)),
    ]
