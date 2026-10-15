from django.core.cache import cache
from django.db import transaction

from .crypto import decrypt, encrypt
from .models import AuditEvent, PolicyDocument, RuntimeSetting, SiteState

SECRET_KEYS = {
    "smtp_password",
    "s3_access_key",
    "s3_secret_key",
    "oauth_github_client_secret",
    "oauth_gitee_client_secret",
    "oauth_gitlab_client_secret",
    "turnstile_secret_key",
    "license_webhook_secret",
    "policy_webhook_secret",
}
SETTING_KEYS = {
    "operator_name",
    "operator_contact_email",
    "smtp_host",
    "smtp_port",
    "smtp_username",
    "smtp_password",
    "smtp_security",
    "smtp_from_email",
    "smtp_verified",
    "s3_endpoint",
    "s3_region",
    "s3_bucket",
    "s3_access_key",
    "s3_secret_key",
    "s3_prefix",
    "s3_verified",
    "turnstile_site_key",
    "turnstile_secret_key",
    "license_source_url",
    "policy_source_url",
    "license_webhook_secret",
    "policy_webhook_secret",
    "operations_emails",
    "restore_schedule",
} | {f"oauth_{p}_{k}" for p in ("github", "gitee", "gitlab") for k in ("client_id", "client_secret")}


def get_setting(key, default=""):
    record = RuntimeSetting.objects.filter(key=key).first()
    return (decrypt(record.value) if record.secret else record.value) if record else default


@transaction.atomic
def set_setting(key, value, secret=False, user=None):
    if key not in SETTING_KEYS:
        raise ValueError("Unsupported runtime setting")
    secret = key in SECRET_KEYS or secret
    RuntimeSetting.objects.update_or_create(
        key=key,
        defaults={
            "value": encrypt(str(value)) if secret else str(value),
            "secret": secret,
            "updated_by": user,
        },
    )
    if key.startswith("smtp_") and key != "smtp_verified":
        RuntimeSetting.objects.filter(key="smtp_verified").delete()
    if key.startswith("s3_") and key != "s3_verified":
        RuntimeSetting.objects.filter(key="s3_verified").delete()
    AuditEvent.objects.create(actor=user, action="setting.updated", subject=key)
    cache.delete("avatar-storage-health")


def policy_languages():
    return [
        language
        for language in PolicyDocument.objects.values_list("language", flat=True).distinct()
        if set(PolicyDocument.objects.filter(language=language).values_list("kind", flat=True))
        >= {"privacy", "terms", "inclusion"}
    ]


def registration_issues():
    issues = []
    if not SiteState.objects.filter(pk=1, activated_at__isnull=False).exists():
        issues.append("站点尚未完成初始化 / Site initialization incomplete")
    if not get_setting("smtp_host") or get_setting("smtp_verified") != "1":
        issues.append("SMTP 尚未通过验证 / SMTP is not verified")
    if not get_setting("turnstile_site_key") or not get_setting("turnstile_secret_key"):
        issues.append("Turnstile 未配置 / Turnstile is not configured")
    if not policy_languages():
        issues.append("必需政策正文缺失 / Required policy documents are missing")
    return issues


def registration_ready():
    return not registration_issues()


def provider_enabled(platform):
    return bool(get_setting(f"oauth_{platform}_client_id") and get_setting(f"oauth_{platform}_client_secret"))
