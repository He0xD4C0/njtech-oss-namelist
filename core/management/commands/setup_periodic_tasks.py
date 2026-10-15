from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from django_celery_beat.models import CrontabSchedule, PeriodicTask


class Command(BaseCommand):
    help = "Install idempotent database-managed synchronization schedules. Run with migrations."

    def handle(self, *args, **options):
        base = urlsplit(settings.SITE_BASE_URL)
        if (
            base.scheme not in {"http", "https"}
            or not base.netloc
            or base.username
            or base.password
            or base.path not in {"", "/"}
            or base.query
            or base.fragment
        ):
            from django.core.management.base import CommandError

            raise CommandError("SITE_BASE_URL must be the canonical site HTTP(S) origin")
        Site.objects.update_or_create(
            pk=settings.SITE_ID, defaults={"domain": base.netloc, "name": "NJTech OSS"}
        )
        Site.objects.clear_cache()
        for name, task, minute, hour in [
            ("source-documents-and-licenses", "core.tasks.daily_source_sync", "17", "3"),
            ("git-heads-hourly", "repositories.tasks.sync_git", "7", "*"),
            ("recover-audit-outbox", "repositories.tasks.recover_audits", "*/5", "*"),
            ("recover-avatar-deletions", "core.tasks.recover_avatar_deletions", "29", "*"),
            ("provider-api-six-hourly", "repositories.tasks.sync_platforms", "0", "*/6"),
            ("daily-star-observations", "directory.tasks.snapshot_stars", "35", "23"),
            ("freeze-ended-months", "directory.tasks.close_months", "10", "0"),
            ("isolated-restore-check", "core.tasks.restore_drill", "0", "5"),
            ("maintenance-backup-readiness", "core.tasks.report_backup_readiness", "*", "*"),
        ]:
            schedule, _ = CrontabSchedule.objects.get_or_create(
                minute=minute,
                hour=hour,
                day_of_week="*",
                day_of_month="*",
                month_of_year="*",
                timezone="Asia/Shanghai",
            )
            PeriodicTask.objects.update_or_create(
                name=name, defaults={"task": task, "crontab": schedule, "interval": None, "args": "[]"}
            )
        self.stdout.write("Synchronization schedules installed (one Beat instance required)")
