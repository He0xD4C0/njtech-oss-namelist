from celery.signals import task_failure
from django.dispatch import receiver


@receiver(task_failure)
def report_task_failure(sender=None, **kwargs):
    if sender and sender.name != "core.tasks.notify_failure":
        from .tasks import notify_failure

        try:
            notify_failure.delay(sender.name)
        except Exception:
            # A down broker cannot carry its own alert. Runtime external monitoring
            # is a separate deployment gate, never claimed by this in-process hook.
            pass
