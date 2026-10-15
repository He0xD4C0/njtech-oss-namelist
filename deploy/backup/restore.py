"""Restore into newly allocated scratch data only; never start application workers."""

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

PG_BIN = Path("/usr/lib/postgresql/17/bin")
SCRATCH_ROOT = Path("/var/lib/restore")


class RestoreCommandError(RuntimeError):
    """Safe phase/exit diagnostic, never subprocess output or credential-bearing arguments."""


def command(arguments, timeout=3600):
    try:
        return subprocess.run(
            [str(value) for value in arguments], check=True, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.CalledProcessError as error:
        operation = next(
            (str(value) for value in arguments if value in {"info", "restore", "start", "stop", "--check"}),
            "inspect",
        )
        raise RestoreCommandError(
            f"{Path(str(arguments[0])).name}/{operation} exit {error.returncode}"
        ) from None


def validate_root(root):
    if root.resolve() != SCRATCH_ROOT or root.is_symlink():
        raise ValueError("Restore work must use the dedicated /var/lib/restore scratch volume")


def remove_scratch(path, root):
    if path.is_symlink() or path.resolve().parent != root.resolve() or not path.name.startswith("drill-"):
        raise ValueError("Refusing to clean an unrecognized restore path")
    shutil.rmtree(path)


def validate_database(socket_directory):
    # Raw read-only SQL: deliberately do not initialize Django, Beat, mail or restored application tasks.
    import psycopg
    from cryptography.fernet import Fernet

    key = os.getenv("APP_ENCRYPTION_KEY", "")
    if not key:
        key = Path(os.environ["APP_ENCRYPTION_KEY_FILE"]).read_text().strip()
    cipher = Fernet(key.encode())
    with psycopg.connect(
        host=str(socket_directory),
        port=55432,
        user=os.getenv("POSTGRES_USER", "namelist"),
        dbname=os.getenv("POSTGRES_DB", "namelist"),
        connect_timeout=10,
        options="-c default_transaction_read_only=on -c statement_timeout=60000",
    ) as database:
        with database.cursor() as cursor:
            cursor.execute("SELECT pg_is_in_recovery()")
            if cursor.fetchone()[0]:
                raise RuntimeError("Restore did not reach a consistent promoted state")
            for table in (
                "django_migrations",
                "core_user",
                "core_runtimesetting",
                "core_pendingavatardeletion",
                "repositories_repository",
                "repositories_submission",
                "repositories_auditdispatch",
            ):
                cursor.execute("SELECT to_regclass(%s)", [table])
                if cursor.fetchone()[0] is None:
                    raise RuntimeError("Required schema is absent")
            cursor.execute("SELECT count(*) FROM django_migrations")
            migration_count = cursor.fetchone()[0]
            cursor.execute("SELECT value FROM core_runtimesetting WHERE secret AND value <> ''")
            secrets = cursor.fetchall()
            if not secrets:
                raise RuntimeError("No encrypted setting exists to prove recovery-key compatibility")
            for (value,) in secrets:
                cipher.decrypt(value.encode())
            return {"migrations": migration_count, "encrypted_settings": len(secrets)}


def restore(args):
    root = Path(os.getenv("RESTORE_SCRATCH_ROOT", str(SCRATCH_ROOT)))
    validate_root(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".restore.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inventory = json.loads(
            command(
                ["pgbackrest", f"--stanza={os.getenv('BACKUP_STANZA', 'namelist')}", "--output=json", "info"],
                timeout=180,
            ).stdout
        )
        backups = [item for stanza in inventory for item in stanza.get("backup", [])]
        backups = [item for item in backups if not args.backup_set or item["label"] == args.backup_set]
        if not backups:
            raise RuntimeError("No completed backup is available")
        selected = max(backups, key=lambda item: item["timestamp"]["stop"])
        size = selected["info"]["size"]
        maximum = int(os.getenv("RESTORE_MAX_BYTES", str(200 * 1024**3)))
        reserve = int(os.getenv("RESTORE_RESERVE_BYTES", str(50 * 1024**3)))
        if size > maximum or shutil.disk_usage(root).free < size * 1.25 + reserve:
            raise RuntimeError("Restore size or free-space safety budget is exceeded")
        scratch = Path(tempfile.mkdtemp(prefix="drill-", dir=root))
        data = scratch / "data"
        socket_directory = scratch / "socket"
        socket_directory.mkdir(mode=0o700)
        started = False
        success = False
        try:
            restore_command = [
                "pgbackrest",
                f"--stanza={os.getenv('BACKUP_STANZA', 'namelist')}",
                f"--pg1-path={data}",
                f"--tablespace-map-all={scratch / 'tablespaces'}",
                f"--lock-path={scratch / 'locks'}",
                "--no-link-all",
                "--archive-mode=off",
                "--target-action=promote",
            ]
            if args.backup_set:
                restore_command.append(f"--set={args.backup_set}")
            else:
                # Pin the inspected backup; a concurrent new backup cannot change the disk budget.
                restore_command.append(f"--set={selected['label']}")
            if args.target_time:
                restore_command += ["--type=time", f"--target={args.target_time}"]
            else:
                restore_command.append("--type=immediate")
            command(
                restore_command + ["restore"],
                timeout=int(os.getenv("RESTORE_TIMEOUT_SECONDS", "14400")) - 900,
            )
            command([PG_BIN / "pg_controldata", data])
            hba = scratch / "pg_hba.conf"
            hba.write_text("local all all trust\n")
            config = scratch / "postgresql.conf"
            config.write_text("listen_addresses = ''\narchive_mode = off\nshared_buffers = '128MB'\n")
            # Every path is newly allocated here. Original data is a read-only volume in this container.
            options = (
                f"-c config_file={config} -c hba_file={hba} -c data_directory={data} "
                f"-c unix_socket_directories={socket_directory} -c unix_socket_permissions=0700 "
                "-c port=55432 -c archive_mode=off -c primary_conninfo= -c logging_collector=off"
            )
            try:
                command(
                    [
                        PG_BIN / "pg_ctl",
                        "-D",
                        data,
                        "-l",
                        scratch / "postgres.log",
                        "-o",
                        options,
                        "-w",
                        "-t",
                        "600",
                        "start",
                    ],
                    timeout=660,
                )
            except RestoreCommandError:
                # Keep one bounded, private startup diagnostic, never a Celery result or public log.
                log = scratch / "postgres.log"
                if log.exists():
                    descriptor = os.open(
                        root / "last-startup-error.log",
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                        0o600,
                    )
                    with os.fdopen(descriptor, "w") as stream:
                        stream.write(log.read_text()[-65536:])
                raise
            started = True
            result = validate_database(socket_directory)
            command([PG_BIN / "pg_ctl", "-D", data, "-m", "fast", "-w", "-t", "60", "stop"], timeout=90)
            started = False
            command([PG_BIN / "pg_checksums", "--check", "--pgdata", data], timeout=1800)
            success = True
            return {
                "status": "ok",
                **result,
                "kept": args.keep,
                "data_directory": str(data) if args.keep else None,
            }
        finally:
            if started or (data / "postmaster.pid").exists():
                try:
                    command(
                        [PG_BIN / "pg_ctl", "-D", data, "-m", "immediate", "-w", "-t", "30", "stop"],
                        timeout=45,
                    )
                except (RestoreCommandError, subprocess.SubprocessError, OSError):
                    # Never remove a directory while its database may still be running.
                    raise RuntimeError(
                        "Scratch PostgreSQL could not stop; retain isolated volume for inspection"
                    )
            if not (args.keep and success):
                remove_scratch(scratch, root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true", help="retain only successfully verified scratch data, stopped"
    )
    parser.add_argument("--backup-set")
    parser.add_argument("--target-time", help="ISO timestamp with UTC offset for point-in-time recovery")
    args = parser.parse_args()
    if args.backup_set and not re.fullmatch(r"\d{8}-\d{6}F(?:_\d{8}-\d{6}[DI])?", args.backup_set):
        parser.error("invalid pgBackRest backup set label")
    if args.target_time and datetime.fromisoformat(args.target_time).utcoffset() is None:
        parser.error("target time must include a UTC offset")
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(InterruptedError("Restore timed out")))
    try:
        print(json.dumps(restore(args)))
    except Exception as error:
        # pgBackRest SQL/log output can contain private configuration. Keep it out of task result storage.
        detail = str(error) if isinstance(error, RestoreCommandError) else type(error).__name__
        raise SystemExit(f"Restore verification failed ({detail}); inspect isolated maintenance logs")


if __name__ == "__main__":
    main()
