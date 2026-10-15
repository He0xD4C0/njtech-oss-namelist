from django.conf import settings
from django.db import models

PLATFORMS = [("github", "GitHub"), ("gitee", "Gitee"), ("gitlab", "GitLab.com")]


class OAuthBinding(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="oauth_bindings")
    platform = models.CharField(max_length=12, choices=PLATFORMS)
    platform_user_id = models.CharField(max_length=100)
    username = models.CharField(max_length=255)
    encrypted_access_token = models.TextField()
    encrypted_refresh_token = models.TextField(blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [  # noqa: RUF012
            models.UniqueConstraint(fields=["platform", "platform_user_id"], name="one_site_user_per_external_id"),
            models.UniqueConstraint(fields=["user", "platform"], name="one_external_id_per_user_platform"),
        ]

    def __str__(self):
        return f"{self.get_platform_display()}: {self.username}"

