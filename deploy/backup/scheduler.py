"""DB-independent backup timer; Celery Beat remains the only application scheduler."""

import json
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

stopping = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stopping.set())
signal.signal(signal.SIGINT, lambda *_: stopping.set())


def due(now, completed):
    scheduled = time(int(os.getenv("BACKUP_HOUR", "3")), int(os.getenv("BACKUP_MINUTE", "15")))
    return now.time().replace(tzinfo=None) >= scheduled and completed != now.date().isoformat()


def notify():
    # SMTP is database-configured. When the DB itself is down, an independent external monitor is required.
    try:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        import django

        django.setup()
        from core.mail import send_system_mail
        from core.runtime import get_setting

        recipients = get_setting("operations_emails").replace(";", ",").split(",")
        recipients = [recipient.strip() for recipient in recipients if recipient.strip()]
        if recipients:
            send_system_mail(
                "[namelist] Backup failed",
                "The scheduled pgBackRest backup failed. Inspect maintenance and pgBackRest logs; credentials are not included in this alert.",
                recipients,
            )
    except Exception:
        print("Backup alert delivery failed; inspect independent monitoring", flush=True)


def main():
    state = Path("/var/lib/restore/backup-state.json")
    try:
        completed = json.loads(state.read_text()).get("completed", "")
    except (FileNotFoundError, ValueError):
        completed = ""
    while not stopping.is_set():
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        from deploy.backup import managed
        if managed.enabled():
            try:
                protected = managed.active()["state"] == "active"
            except (OSError, ValueError):
                protected = False
            if not protected:
                stopping.wait(30)
                continue
        if due(now, completed):
            try:
                subprocess.run(
                    ["/app/deploy/backup/backup.sh", "full" if now.weekday() == 6 else "diff"],
                    check=True,
                    timeout=10800,
                )
                completed = now.date().isoformat()
                temporary = state.with_suffix(".tmp")
                temporary.write_text(json.dumps({"completed": completed}))
                temporary.replace(state)
            except (subprocess.SubprocessError, OSError) as error:
                if isinstance(error, subprocess.CalledProcessError) and error.returncode == 75:
                    stopping.wait(30)
                    continue
                print("Scheduled backup failed; retrying in one hour", flush=True)
                notify()
                stopping.wait(3600)
                continue
        stopping.wait(30)


if __name__ == "__main__":
    main()
