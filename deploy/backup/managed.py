"""Database-independent application-managed backup configuration and locking."""

import fcntl
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager, nullcontext
from pathlib import Path

ROOT = Path(os.getenv("BACKUP_CONFIG_ROOT", "/var/lib/namelist-backup"))
LOCK_ROOT = Path(os.getenv("BACKUP_LOCK_ROOT", "/var/spool/pgbackrest/lock"))


def enabled():
    mode = os.getenv("BACKUP_CONFIG_MODE", "server")
    if mode not in {"server", "application"}:
        raise ValueError("Invalid backup configuration mode")
    return mode == "application"


@contextmanager
def lock(name="operation", *, shared=False, blocking=True):
    LOCK_ROOT.mkdir(parents=True, exist_ok=True)
    with (LOCK_ROOT / f"managed-{name}.lock").open("a") as handle:
        flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(handle, flags | (0 if blocking else fcntl.LOCK_NB))
        yield


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def initialize():
    """Never reconstruct a disabled state after a previously enabled configuration is lost."""
    with lock("archive"):
        if not (ROOT / "active.json").exists() and not (LOCK_ROOT / "managed-enabled").exists():
            write_json(ROOT / "active.json", {"state": "disabled"})


def active(*, allow_pending=False):
    data = json.loads((ROOT / "active.json").read_text())
    if data.get("state") == "disabled":
        if (LOCK_ROOT / "managed-enabled").exists() and not allow_pending:
            raise ValueError("Previously enabled backup configuration is missing")
        return data
    if data.get("state") != "active" or not data.get("revision") or not data.get("values"):
        raise ValueError("Invalid active backup configuration")
    if not config_path(data["revision"]).is_file():
        raise ValueError("Active backup configuration file is missing")
    return data


def config_path(revision):
    import uuid
    return ROOT / "versions" / f"{uuid.UUID(str(revision))}.conf"


def store_config(revision, content):
    path = config_path(revision)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".config-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def activate(data, *, archive_locked=False):
    # Caller holds the operation lock; archiving cannot cross the state transition.
    with (nullcontext() if archive_locked else lock("archive")):
        write_json(LOCK_ROOT / "managed-enabled", {"enabled": True})
        write_json(ROOT / "active.json", data)


def command(operation, *, revision=None, extra=(), timeout=10800):
    if revision is None:
        state = active()
        if state["state"] != "active":
            raise ValueError("Backups are not configured")
        revision = state["revision"]
    return subprocess.run(
        ["pgbackrest", f"--config={config_path(revision)}", "--stanza=namelist", operation, *extra],
        check=True, capture_output=True, text=True, timeout=timeout,
    )


def status():
    try:
        return json.loads((ROOT / "status.json").read_text())
    except FileNotFoundError:
        return {}


def record(**changes):
    # Writers hold the operation lock. Readers see complete snapshots only.
    write_json(ROOT / "status.json", status() | changes)


def backup(kind="full"):
    from datetime import datetime, timezone
    state = active()
    previous = status()
    record(state="running", revision=state["revision"], error="")
    try:
        command("check", timeout=180)
        if kind == "diff":
            import time
            records = json.loads(command("info", extra=("--output=json",), timeout=120).stdout)
            fulls = [b["timestamp"]["stop"] for s in records for b in s.get("backup", []) if b["type"] == "full"]
            if not fulls or time.time() - max(fulls) >= 7 * 86400:
                kind = "full"
        command("backup", extra=(f"--type={kind}",))
        records = json.loads(command("info", extra=("--output=json",), timeout=120).stdout)
        latest = max((b for s in records for b in s.get("backup", [])), key=lambda b: b["timestamp"]["stop"])
        record(state="success", revision=state["revision"], last_success=datetime.fromtimestamp(
            latest["timestamp"]["stop"], timezone.utc).isoformat(), label=latest["label"], error="")
    except Exception:
        record(state="failed", error="backup-failed", last_success=previous.get("last_success", ""))
        raise
