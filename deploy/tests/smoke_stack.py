"""Explicit local integration run. Owns only the njtech-ops-smoke Compose project and deletes its volumes."""

import json
import os
import secrets
import socket
import subprocess
import tempfile
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "njtech-ops-smoke"
urlopen = build_opener(ProxyHandler({})).open


def main():
    environment = os.environ | {
        "APP_IMAGE": "njtech-ops-web:local",
        "SCANNER_IMAGE": "njtech-ops-scanner:local",
        "POSTGRES_BACKUP_IMAGE": "njtech-ops-postgres:local",
        "MAINTENANCE_IMAGE": "njtech-ops-maintenance:local",
        "STATIC_IMAGE": "njtech-ops-static:local",
    }
    existing = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"],
        check=True,
        capture_output=True,
        text=True,
    )
    if existing.stdout.strip():
        raise RuntimeError("The smoke project already has containers; inspect ownership before cleanup/reuse")
    for port in (18000, 18001, 18025):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    with tempfile.TemporaryDirectory(prefix="njtech-ops-smoke-") as directory:
        scratch = Path(directory)
        cipher = scratch / "cipher"
        cipher.write_text(secrets.token_urlsafe(48))
        cipher.chmod(0o444)
        backup = {
            "BACKUP_ENABLED": "1",
            "BACKUP_REPO_TYPE": "posix",
            "BACKUP_REPO_PATH": "/var/lib/pgbackrest/smoke",
            "BACKUP_CIPHER_FILE": "/run/secrets/backup_cipher",
            "RESTORE_RESERVE_BYTES": str(100 * 1024**2),
        }
        overlay = scratch / "compose-smoke.json"
        overlay.write_text(
            json.dumps(
                {
                    "services": {
                        "db": {"environment": backup, "secrets": ["backup_cipher"]},
                        "maintenance": {"environment": backup, "secrets": ["backup_cipher"]},
                    },
                    "secrets": {"backup_cipher": {"file": str(cipher)}},
                }
            )
        )
        compose = [
            "docker",
            "compose",
            "--profile",
            "ops",
            "--project-name",
            PROJECT,
            "-f",
            str(ROOT / "compose.yaml"),
            "-f",
            str(ROOT / "deploy/compose.local.yaml"),
            "-f",
            str(overlay),
        ]

        def run(*arguments, timeout=600, capture=False):
            try:
                return subprocess.run(
                    compose + list(arguments),
                    cwd=ROOT,
                    env=environment,
                    check=True,
                    text=True,
                    capture_output=capture,
                    timeout=timeout,
                )
            except subprocess.CalledProcessError as error:
                if capture:
                    print(error.stderr)
                raise

        created = False
        try:
            created = True
            run(
                "up",
                "--no-build",
                "--detach",
                "--wait",
                "--wait-timeout",
                "300",
                "db",
                "redis",
                "initialize",
                "web",
                "static",
                "localproxy",
                "mailpit",
            )
            with urlopen("http://localhost:18000/", timeout=20) as response:
                assert response.status == 200 and b"<!" in response.read()
            with urlopen(
                "http://localhost:18000/static/vendor/tabler/tabler.min.css", timeout=20
            ) as response:
                assert response.status == 200 and b"Tabler" in response.read(1024)
            marker = "smoke-credential-" + secrets.token_hex(16)
            static_request = Request(
                f"http://localhost:18000/static/vendor/tabler/tabler.min.css?code={marker}",
                headers={"Referer": f"http://localhost:18000/accounts/confirm-email/{marker}/"},
            )
            with urlopen(static_request, timeout=20) as response:
                assert response.status == 200
            with urlopen(f"http://localhost:18000/?code={marker}", timeout=20) as response:
                assert response.status == 200
            # Local-only sentinel: proves the retained application root key can decrypt the restored DB.
            run(
                "run",
                "--rm",
                "--no-deps",
                "web",
                "python",
                "manage.py",
                "shell",
                "-c",
                "from core.runtime import set_setting; set_setting('smtp_password', 'local-restore-sentinel'); set_setting('restore_schedule', 'off')",
            )
            # Exercise a real captured SMTP delivery without asserting internet deliverability.
            run(
                "run",
                "--rm",
                "--no-deps",
                "web",
                "python",
                "manage.py",
                "shell",
                "-c",
                "from core.runtime import set_setting; from django.core.mail import send_mail; "
                "set_setting('smtp_host','mailpit'); set_setting('smtp_port','1025'); set_setting('smtp_security','none'); "
                "send_mail('Local smoke','Local SMTP only','smoke@example.invalid',['operator@example.invalid'])",
            )
            with urlopen("http://localhost:18025/api/v1/messages", timeout=20) as response:
                assert json.load(response)["total"] >= 1
            logs = run("logs", "--no-color", "web", "static", "localproxy", capture=True)
            assert marker not in logs.stdout + logs.stderr, "Credential marker leaked into access logs"
            run(
                "run",
                "--rm",
                "--no-deps",
                "maintenance",
                "/app/deploy/backup/backup.sh",
                "full",
                timeout=1800,
            )
            run(
                "run",
                "--rm",
                "--no-deps",
                "maintenance",
                "/app/deploy/backup/backup.sh",
                "diff",
                timeout=1800,
            )
            # Prove recovery does not depend on the live database, broker, or application.
            run("stop", "web", "db", "redis", "localproxy")
            result = run(
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "/app/deploy/backup/restore-drill.sh",
                "maintenance",
                timeout=1800,
                capture=True,
            )
            evidence = json.loads(result.stdout.strip())
            assert evidence["status"] == "ok" and evidence["encrypted_settings"] >= 1
            assert evidence["kept"] is False
            print(
                json.dumps(
                    {
                        "compose_smoke": "passed",
                        "http": 200,
                        "static": 200,
                        "mailpit": "captured",
                        "access_logs": "credential-markers-absent",
                        "encrypted_backup": "full+diff",
                        "offline_restore": evidence,
                    },
                    indent=2,
                )
            )
        except Exception:
            # This project contains only generated local credentials, never production integrations.
            run("logs", "--no-color", "--tail", "100", "initialize", "db", "web", "localproxy", timeout=30)
            run(
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "sh",
                "maintenance",
                "-c",
                "if [ -f /var/lib/restore/last-startup-error.log ]; then cat /var/lib/restore/last-startup-error.log; fi",
                timeout=30,
            )
            raise
        finally:
            if created:
                # Explicitly scoped project; never runs down on main's standalone or production projects.
                run("down", "--volumes", "--remove-orphans", timeout=180)
                for resource in ("volume", "network"):
                    remaining = subprocess.run(
                        [
                            "docker",
                            resource,
                            "ls",
                            "--quiet",
                            "--filter",
                            f"label=com.docker.compose.project={PROJECT}",
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if remaining.stdout.strip():
                        raise RuntimeError(f"Smoke project retained a {resource}; inspect before reuse")


if __name__ == "__main__":
    main()
