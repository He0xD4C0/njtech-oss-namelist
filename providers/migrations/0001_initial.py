import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]  # noqa: RUF012
    operations = [  # noqa: RUF012
        migrations.CreateModel(
            name="OAuthBinding",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("platform", models.CharField(choices=[("github", "GitHub"), ("gitee", "Gitee"), ("gitlab", "GitLab.com")], max_length=12)),
                ("platform_user_id", models.CharField(max_length=100)),
                ("username", models.CharField(max_length=255)),
                ("encrypted_access_token", models.TextField()),
                ("encrypted_refresh_token", models.TextField(blank=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="oauth_bindings", to=settings.AUTH_USER_MODEL)),
            ],
            options={"constraints": [
                models.UniqueConstraint(fields=("platform", "platform_user_id"), name="one_site_user_per_external_id"),
                models.UniqueConstraint(fields=("user", "platform"), name="one_external_id_per_user_platform"),
            ]},
        ),
    ]

