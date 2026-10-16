"""Presentation state for the setup wizard; saved configuration remains authoritative."""


from .i18n import text
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
    "operations": ("branding", "backup", "operations"), "review": (),
}


def activation_checks(state):
    return [
        {"label": text("邮件服务发送验证", "Mail delivery verified"), "step": "mail", "task": "smtp",
         "ready": bool(get_setting("smtp_host") and get_setting("smtp_verified") == "1")},
        {"label": text("唯一超管 admin 已创建", "The only superuser admin is ready"), "step": "mail", "task": "admin",
         "ready": bool(state.bootstrap_user_id and state.bootstrap_user.username == "admin"
                       and state.bootstrap_user.is_superuser and state.bootstrap_user.is_active)},
        {"label": text("安全验证配置", "Security verification configured"), "step": "requirements", "task": "turnstile",
         "ready": bool(get_setting("turnstile_site_key") and get_setting("turnstile_secret_key"))},
        {"label": text("必需政策正文", "Required policy documents"), "step": "requirements", "task": "documents",
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


# Ordered work items within the six public stages. Optional items never block
# activation; their saved configuration remains the source of truth.
TASKS = (
    ("smtp", "mail", "连接邮件服务", "Connect mail service", True),
    ("admin", "mail", "创建初始管理员", "Create administrator", True),
    ("turnstile", "requirements", "配置安全验证", "Set up security verification", True),
    ("documents", "requirements", "同步政策正文", "Sync policy documents", True),
    ("licenses", "requirements", "配置许可证策略", "Set up license policy", False),
    ("github", "integrations", "绑定 GitHub", "Connect GitHub", False),
    ("gitee", "integrations", "绑定 Gitee", "Connect Gitee", False),
    ("gitlab", "integrations", "绑定 GitLab", "Connect GitLab", False),
    ("storage", "integrations", "配置头像存储", "Set up avatar storage", False),
    ("branding", "operations", "站点 Logo", "Site logo", False),
    ("backup", "operations", "数据库备份", "Database backups", False),
    ("operations", "operations", "设置运维选项", "Set operations preferences", False),
    ("review", "review", "检查并激活", "Review and activate", True),
)

SYNC_LABELS = {
    "pending": ("来源已保存，尚未同步", "Source saved; not synced yet"),
    "queued": ("请求已提交，正在等待执行", "Queued; waiting for a worker"),
    "running": ("正在获取并检查内容", "Fetching and checking content"),
    "retrying": ("遇到临时问题，正在自动重试", "Temporary failure; retrying automatically"),
    "success": ("本次同步已完成", "This sync completed"),
    "unchanged": ("检查完成，已是最新内容", "Checked; already up to date"),
    "failed": ("同步失败，保留上次有效内容。请检查来源或稍后重试。", "Sync failed. Previous valid content is retained. Check the source or retry later."),
    "queue-failed": ("任务未能提交，请重试；持续失败时请检查任务服务。", "Could not queue the task. Retry, or check the worker service if this persists."),
    "superseded": ("此请求已被新的配置或请求替代", "Superseded by newer configuration or a newer request"),
    "not-configured": ("尚未配置来源", "No source configured"),
}


def recovery_label(state):
    labels = {
        "executing": ("执行中，执行者正在续期。", "Running; the worker is renewing its lease."),
        "waiting": ("执行者暂未续期，正在等待原执行权失效。", "The worker has not renewed its lease. Waiting for ownership to expire."),
        "recoverable": ("任务已中断或失败，可以重新尝试；已保存的配置和有效内容会保留。", "The task was interrupted or failed. You can retry; saved settings and valid content are retained."),
    }
    return text(*labels[state]) if state in labels else ""


def wizard_tasks(state, skipped=()):
    from repositories.models import LicensePolicy

    from .backup import public_status
    from .i18n import text
    from .models import RuntimeSetting, SyncRun
    from .runtime import provider_enabled
    from .sync import recovery_state
    backup = public_status()
    checks = activation_checks(state)
    runs = {kind: SyncRun.objects.filter(kind=kind).order_by("-created_at", "-pk").first()
            for kind in ("documents", "licenses")}
    ready = {
        "smtp": checks[0]["ready"], "admin": checks[1]["ready"],
        "turnstile": checks[2]["ready"],
        "documents": checks[3]["ready"],
        "licenses": LicensePolicy.objects.filter(active=True).exists(),
        **{p: provider_enabled(p) for p in ("github", "gitee", "gitlab")},
        "storage": get_setting("s3_verified") == "1",
        "operations": RuntimeSetting.objects.filter(key="restore_schedule").exists(),
        "branding": bool(get_setting("site_logo_revision")),
        "backup": backup["enabled"],
        "review": False,
    }
    for kind, run in runs.items():
        if run and run.status not in {"success", "unchanged"}:
            ready[kind] = False
    summary_values = dict(RuntimeSetting.objects.filter(secret=False, key__in=(
        "smtp_host", "smtp_port", "smtp_from_email", "policy_source_url", "license_source_url",
        "s3_endpoint", "s3_bucket", "operations_emails",
    )).values_list("key", "value"))
    summaries = {
        "smtp": f'{summary_values.get("smtp_host", "")}:{summary_values.get("smtp_port", "")} · {summary_values.get("smtp_from_email", "")}',
        "admin": state.bootstrap_user.username if state.bootstrap_user_id else "",
        "turnstile": text("站点公钥和验证密钥已配置", "Site key and verification secret configured"),
        "documents": summary_values.get("policy_source_url", ""),
        "licenses": summary_values.get("license_source_url", ""),
        "storage": f'{summary_values.get("s3_endpoint", "")} · {summary_values.get("s3_bucket", "")}',
        "branding": text("自定义 Logo 已应用", "Custom logo applied"),
        "backup": backup["label"],
        "operations": summary_values.get("operations_emails") or text("未设置通知邮箱", "No notification recipients"),
        **{p: text("应用凭据已配置", "Application credentials configured") for p in ("github", "gitee", "gitlab")},
    }
    tasks, blocked = [], False
    for key, step, zh, en, required in TASKS:
        run = runs.get(key)
        recovery = recovery_state(run) if run else ""
        recovery_hint = recovery_label(recovery) if recovery != "executing" else ""
        tasks.append({"key": key, "step": step, "title": text(zh, en), "required": required,
                      "summary": summaries.get(key, "") if ready[key] else "",
                      "ready": ready[key], "locked": blocked,
                      "skipped": key in skipped and not ready[key],
                      "url": f"/setup/?step={step}&task={key}",
                      "run": run, "recovery": recovery, "recoverable": recovery == "recoverable",
                      "sync_label": recovery_hint or (text(*SYNC_LABELS.get(run.status, ("状态未知", "Unknown status"))) if run else ""),
                      "waiting": (key == "backup") or bool(run and run.status in {"queued", "running", "retrying"})})
        if required and not ready[key]:
            blocked = True
    return tasks
