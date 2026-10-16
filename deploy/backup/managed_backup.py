#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from deploy.backup import managed  # noqa: E402

try:
    if sys.argv[1:] not in (["full"], ["diff"]):
        raise ValueError("Invalid backup type")
    with managed.lock(blocking=False):
        managed.backup(sys.argv[1])
except BlockingIOError:
    sys.exit(75)
except Exception:
    sys.exit("Database backup failed; inspect protected maintenance logs and storage access.")
