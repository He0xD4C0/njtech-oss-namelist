"""Explicitly isolated fault-test worker; replaces only external Git/ScanCode work."""
# ruff: noqa: E402 -- Django must initialize before importing application models.
import os
import time
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from celery import current_task
from django.conf import settings
from django.utils import timezone

from config.celery import app
from core import git
from licensecheck import engine

if os.environ.get("NAMELIST_RECOVERY_FIXTURE") != "1" or settings.SITE_BASE_URL != "http://localhost:18003":
    raise RuntimeError("This worker may run only in the isolated local recovery acceptance stack")

root = Path(settings.WORK_ROOT) / "recovery-fixtures"
root.mkdir(parents=True, exist_ok=True)


def snapshot(source, *, mode):
    run_id = current_task.request.kwargs["run_id"]
    marker = root / f"{run_id}.started"
    with (root / f"{run_id}.attempts").open("a") as stream:
        stream.write(f"{os.getpid()}\n")
    if not marker.exists():
        marker.write_text(str(os.getpid()))
        while not (root / f"{run_id}.release").exists():
            time.sleep(0.1)
    files = {f"zh-CN/{kind}.md": "Recovery fixture policy" for kind in ("privacy", "terms", "inclusion")}
    if mode == "rules":
        files = {"whitelist/MIT.txt": "Local fixture; not a ScanCode acceptance"}
    return git.GitSnapshot("a" * 40, timezone.now(), files)


git.fetch_snapshot = snapshot
engine.compile_policy = lambda files, path: {"detector_version": "recovery-fixture"}
engine.detector_version = lambda: "recovery-fixture"
engine.validate_policy_artifacts = lambda *args: None

app.worker_main(["worker", "--loglevel=INFO", "--queues=celery,scan", "--concurrency=2", "--without-gossip", "--without-mingle"])
