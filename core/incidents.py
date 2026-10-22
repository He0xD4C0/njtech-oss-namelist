"""Durable incident aggregation and mail outbox; never persist exception bodies."""

import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .mail import send_system_mail
from .models import Incident, NotificationDelivery
from .runtime import get_setting


def _wake(pk):
    try:
        deliver_notification.delay(pk)
    except Exception:
        pass  # The periodic outbox recovery owns broker outages.


def _notification(key, kind, incidents):
    delivery, created = NotificationDelivery.objects.get_or_create(key=key, defaults={"kind": kind})
    if created:
        delivery.incidents.set(incidents)
        transaction.on_commit(lambda: _wake(delivery.pk))
    return delivery


@transaction.atomic
def report(key, kind, summary, affected=()):
    incident, created = Incident.objects.get_or_create(
        key=key, resolved_at=None, defaults={"kind": kind, "summary": summary},
    )
    incident = Incident.objects.select_for_update().get(pk=incident.pk)
    incident.summary = summary
    incident.affected = sorted(set(incident.affected) | set(affected))
    incident.save(update_fields=["summary", "affected", "last_seen_at"])
    if created:
        _notification(f"first:{incident.pk}", "first", [incident])
    return incident


@transaction.atomic
def resolve(key):
    incident = Incident.objects.select_for_update().filter(key=key, resolved_at=None).first()
    if incident:
        incident.resolved_at = timezone.now()
        incident.save(update_fields=["resolved_at"])
        _notification(f"recovery:{incident.pk}", "recovery", [incident])


def provider_key(binding):
    return f"provider:{binding.user_id}:{binding.platform}"


def report_provider(binding, error):
    from repositories.models import Submission
    affected = list(Submission.objects.filter(user_id=binding.user_id, repository__platform=binding.platform)
                    .exclude(status="removed").values_list("repository__full_name", flat=True))
    reasons = {
        "reauthorization": "平台账号授权失效，等待重新授权。",
        "configuration": "平台应用或加密配置异常，需要管理员修复。",
        "rate_limit": "平台限流，正在有限退避重试。",
    }
    return report(provider_key(binding), "provider", reasons.get(error.code, "平台临时故障。"), affected)


@shared_task
def daily_digest():
    now = timezone.now().astimezone(ZoneInfo("Asia/Shanghai"))
    with transaction.atomic():
        incidents = list(Incident.objects.filter(resolved_at=None))
        if not incidents:
            return "empty"
        return _notification(f"digest:{now.date()}", "digest", incidents).pk


@shared_task
def recover_notifications():
    now = timezone.now()
    jobs = NotificationDelivery.objects.exclude(status__in=["sent", "skipped"]).filter(attempts__lt=8).filter(
        Q(retry_at=None) | Q(retry_at__lte=now),
    ).filter(Q(lease_expires_at=None) | Q(lease_expires_at__lte=now))
    for pk in jobs.values_list("pk", flat=True):
        _wake(pk)


@shared_task
def deliver_notification(pk):
    now = timezone.now()
    with transaction.atomic():
        delivery = NotificationDelivery.objects.select_for_update().get(pk=pk)
        if delivery.status in {"sent", "skipped"} or delivery.attempts >= 8:
            return delivery.status
        if (delivery.lease_expires_at and delivery.lease_expires_at > now) or (delivery.retry_at and delivery.retry_at > now):
            return "waiting"
        delivery.execution_token = uuid.uuid4()
        delivery.lease_expires_at = now + timedelta(minutes=5)
        delivery.attempts += 1
        delivery.status = "sending"
        delivery.save()
    recipients = [s.strip() for s in get_setting("operations_emails").split(",") if s.strip()]
    incidents = delivery.incidents.all()
    if delivery.kind == "digest":
        incidents = incidents.filter(resolved_at=None)
        if not incidents.exists():
            NotificationDelivery.objects.filter(pk=pk, execution_token=delivery.execution_token).update(
                status="skipped", reason="发送前故障均已恢复，本日汇总取消。", execution_token=None, lease_expires_at=None,
            )
            return "skipped"
    title = {"first": "首次故障通知", "digest": "每日未恢复故障汇总", "recovery": "故障恢复通知"}[delivery.kind]
    body = "\n\n".join(
        f"故障 #{i.pk} · {i.summary}\n受影响对象：{'、'.join(i.affected) or '后台任务'}"
        + ("\n状态：已恢复" if i.resolved_at else "\n状态：尚未恢复")
        for i in incidents
    )
    try:
        if not recipients:
            raise ValueError("No recipients")
        if send_system_mail(
            f"NJTech OSS · {title}", body, recipients,
            action_url=settings.SITE_BASE_URL.rstrip("/") + "/admin/core/incident/", action_label="查看故障记录",
        ) != 1:
            raise ConnectionError("Mail not accepted")
    except Exception:
        NotificationDelivery.objects.filter(pk=pk, execution_token=delivery.execution_token).update(
            status="failed", reason="未配置收件人" if not recipients else "邮件服务未确认投递，可在记录中追踪重试。",
            retry_at=now + timedelta(seconds=min(3600, 60 * 2 ** delivery.attempts)),
            execution_token=None, lease_expires_at=None,
        )
        return "failed"
    NotificationDelivery.objects.filter(pk=pk, execution_token=delivery.execution_token).update(
        status="sent", sent_at=timezone.now(), reason="邮件服务已接受投递", execution_token=None, lease_expires_at=None,
    )
    return "sent"
