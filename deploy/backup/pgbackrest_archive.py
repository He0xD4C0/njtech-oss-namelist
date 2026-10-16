#!/usr/bin/env python3
"""Fixed archive command: fail closed after backup protection has been enabled."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from deploy.backup import managed  # noqa: E402

try:
    with managed.lock("archive", shared=True):
        state = managed.active()
        if state["state"] == "active":
            managed.command("archive-push", revision=state["revision"], extra=(sys.argv[1],), timeout=180)
except Exception:
    sys.exit("WAL archive failed; retained for retry. Check backup configuration and service health.")
