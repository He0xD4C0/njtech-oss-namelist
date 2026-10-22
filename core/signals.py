from celery.signals import task_failure
from django.dispatch import receiver


@receiver(task_failure)
def report_task_failure(sender=None, exception=None, **kwargs):
    if not sender or sender.name in {"core.tasks.notify_failure", "core.incidents.deliver_notification"}:
        return
    if getattr(exception, "incident_recorded", False):
        return
    from .incidents import report
    # Source sync uses the same durable key as the business failure path.
    kind = {"core.tasks.sync_documents": "documents", "core.tasks.sync_license_policy": "licenses",
            "core.tasks.restore_drill": "restore"}.get(sender.name, sender.name)
    key = f"task:{kind}"
    args = kwargs.get("args") or ()
    if args and sender.name == "repositories.tasks.audit_submission":
        key, kind = f"audit:{args[0]}", "audit"
    elif args and sender.name == "repositories.approvals.process_approval":
        key, kind = f"approval:{args[0]}", "license-approval"
    report(key, kind[:40], "任务执行失败，请查看后台脱敏运行记录。")
