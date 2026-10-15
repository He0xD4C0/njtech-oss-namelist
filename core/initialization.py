"""Presentation state for the setup wizard; saved configuration remains authoritative."""

from allauth.account.models import EmailAddress

from .runtime import get_setting, policy_languages

SETUP_STEPS = (
    ("access", "引导授权", "Authorize"),
    ("mail", "邮件与管理员", "Mail and administrator"),
    ("requirements", "安全与策略", "Security and policies"),
    ("integrations", "平台与存储", "Platforms and storage"),
    ("operations", "运维选项", "Operations"),
    ("review", "检查并激活", "Review and activate"),
)
STEP_GROUPS = {
    "access": (), "mail": ("smtp",),
    "requirements": ("turnstile", "documents", "licenses"),
    "integrations": ("github", "gitee", "gitlab", "storage"),
    "operations": ("operations",), "review": (),
}


def activation_checks(state):
    return [
        {"label": "SMTP 发送验证 / SMTP delivery", "step": "mail",
         "ready": bool(get_setting("smtp_host") and get_setting("smtp_verified") == "1")},
        {"label": "初始管理员邮箱 / Initial administrator email", "step": "mail",
         "ready": bool(state.bootstrap_user_id and EmailAddress.objects.filter(
             user_id=state.bootstrap_user_id, email__iexact=state.bootstrap_user.email, verified=True
         ).exists())},
        {"label": "Turnstile 安全验证配置 / Turnstile configuration", "step": "requirements",
         "ready": bool(get_setting("turnstile_site_key") and get_setting("turnstile_secret_key"))},
        {"label": "必需政策正文 / Required policy documents", "step": "requirements",
         "ready": bool(policy_languages())},
    ]


def backup_status():
    from django.core.cache import cache

    try:
        report = cache.get("backup-readiness") or {}
    except Exception:
        report = {}
    status = report.get("state")
    return {
        "ready": "外部备份配置已就绪 / External backup configuration ready",
        "disabled": "外部备份未启用 / External backups disabled",
        "not-configured": "外部备份未配置完整 / External backup configuration incomplete",
        "invalid": "外部备份配置无效，请检查服务器配置 / Invalid external backup configuration",
    }.get(status, "尚未收到维护服务的有效状态；请检查维护服务与调度器。 / No current maintenance status; check the maintenance service and scheduler.")
