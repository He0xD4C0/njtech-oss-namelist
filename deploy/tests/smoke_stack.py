"""Explicit local integration run. Owns only the njtech-ops-smoke Compose project and deletes its volumes."""

import gzip
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "njtech-ops-smoke"
urlopen = build_opener(ProxyHandler({})).open


def main(configured):
    environment = os.environ | {
        "APP_IMAGE": "njtech-ops-web:local",
        "SCANNER_IMAGE": "njtech-ops-scanner:local",
        "POSTGRES_BACKUP_IMAGE": "njtech-ops-postgres:local",
        "MAINTENANCE_IMAGE": "njtech-ops-maintenance:local",
    }
    existing = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"],
        check=True,
        capture_output=True,
        text=True,
    )
    if existing.stdout.strip():
        raise RuntimeError("The smoke project already has containers; inspect ownership before cleanup/reuse")
    for port in (18000, 18025):
        with socket.socket() as probe:
            # Sequential scenarios can leave this process's HTTP sockets in TIME_WAIT.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
        if not configured:
            backup = {
                "BACKUP_ENABLED": "1", "BACKUP_REPO_TYPE": "s3",
                "BACKUP_S3_ENDPOINT": "", "BACKUP_S3_BUCKET": "", "BACKUP_S3_REGION": "",
            }
        overlay = scratch / "compose-smoke.json"
        overlay.write_text(
            json.dumps(
                {
                    "services": {
                        "db": {"environment": backup, "secrets": ["backup_cipher"] if configured else []},
                        "maintenance": {"environment": backup, "secrets": ["backup_cipher"] if configured else []},
                        "web": {"environment": {"DJANGO_DEBUG": "0"}},
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
                "mailpit",
            )
            with urlopen("http://localhost:18000/", timeout=20) as response:
                assert response.status == 200 and b"<!" in response.read()
            # Production Gunicorn + WhiteNoise, not Django's development static handler.
            for path, mime in (
                ("vendor/tabler/tabler.min.css", "text/css"),
                ("vendor/tabler/tabler.min.js", "text/javascript"),
                ("vendor/easymde/easymde.min.js", "text/javascript"),
                ("vendor/fontawesome/fonts/fontawesome-webfont.woff2", "font/woff2"),
                ("img/default-avatar.svg", "image/svg+xml"),
                ("admin/css/base.css", "text/css"),
                ("unfold/css/styles.css", "text/css"),
            ):
                with urlopen(f"http://localhost:18000/static/{path}", timeout=20) as response:
                    assert response.status == 200 and response.read()
                    assert response.headers.get_content_type() == mime, (path, response.headers)
                    assert response.headers["Cache-Control"] == "max-age=60, public"
            css = "http://localhost:18000/static/vendor/tabler/tabler.min.css"
            with urlopen(Request(css, headers={"Accept-Encoding": "gzip"}), timeout=20) as response:
                assert response.headers["Content-Encoding"] == "gzip"
                assert b"Tabler" in gzip.decompress(response.read())
                etag, modified = response.headers["ETag"], response.headers["Last-Modified"]
            with urlopen(Request(css, method="HEAD"), timeout=20) as response:
                assert response.status == 200 and response.read() == b""
                assert int(response.headers["Content-Length"]) > 0
            for headers, expected in (
                ({"If-None-Match": etag}, 304),
                ({"If-Modified-Since": modified}, 304),
            ):
                try:
                    urlopen(Request(css, headers=headers), timeout=20)
                except HTTPError as error:
                    assert error.code == expected
                    error.close()
                else:
                    raise AssertionError("Conditional static request must return 304")
            for path in ("/static/not-present.css", "/static/../config/settings.py", "/media/not-present.png"):
                try:
                    urlopen(f"http://localhost:18000{path}", timeout=20)
                except HTTPError as error:
                    assert error.code == 404
                    error.close()
                else:
                    raise AssertionError("Non-static files must not be served")
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
            browser_smoke(run)
            logs = run("logs", "--no-color", "web", capture=True)
            assert marker not in logs.stdout + logs.stderr, "Credential marker leaked into access logs"
            mode = run("exec", "-T", "-u", "postgres", "db", "psql", "-d", "postgres", "-Atc", "SHOW archive_mode", capture=True).stdout.strip()
            assert mode == ("on" if configured else "off"), mode
            if not configured:
                run("up", "--no-build", "--detach", "maintenance")
                run(
                    "exec", "-T", "maintenance", "python", "manage.py", "shell", "-c",
                    "from core.tasks import restore_drill; from core.runtime import set_setting; "
                    "from core.models import SyncRun; from django.core.cache import cache; "
                    "before=SyncRun.objects.count(); "
                    "set_setting('restore_schedule','weekly'); assert restore_drill()=='not-configured'; "
                    "set_setting('restore_schedule','monthly'); assert restore_drill()=='not-configured'; "
                    "set_setting('restore_schedule','quarterly'); assert restore_drill()=='not-configured'; "
                    "assert SyncRun.objects.count()==before; assert not cache.get('restore-drill-running')",
                )
                run(
                    "exec", "-T", "maintenance", "python", "-c",
                    "import subprocess; from pathlib import Path; "
                    "assert not Path('/etc/pgbackrest/pgbackrest.conf').exists(); "
                    "assert not Path('/var/lib/restore/backup-state.json').exists(); "
                    "assert not list(Path('/var/lib/restore').glob('drill-*')); "
                    "results=[subprocess.run(c, capture_output=True) for c in "
                    "[['/app/deploy/backup/backup.sh','full'],['/app/deploy/backup/restore-drill.sh']]]; "
                    "assert all(r.returncode==2 for r in results)",
                )
                run(
                    "exec", "-T", "maintenance", "python", "-c",
                    "from pathlib import Path; "
                    "assert not any(b'/app/deploy/backup/scheduler.py' in p.read_bytes().split(bytes([0])) "
                    "for p in Path('/proc').glob('[0-9]*/cmdline') if p.exists())",
                )
                print(json.dumps({"unconfigured_smoke": "passed", "archive_mode": mode, "restore": "not-configured", "scheduler": "absent"}))
                return
            run(
                "run",
                "--rm",
                "--no-deps",
                "maintenance",
                "/app/deploy/backup/backup.sh",
                "full",
                timeout=1800,
            )
            # A configured repository failure must retain WAL, never switch archiving off.
            original_mode = run("exec", "-T", "db", "stat", "-c", "%a", "/var/lib/pgbackrest/smoke", capture=True).stdout.strip()
            run("exec", "-T", "db", "chmod", "000", "/var/lib/pgbackrest/smoke")
            try:
                run("exec", "-T", "-u", "postgres", "db", "psql", "-d", "postgres", "-Atc", "SELECT pg_create_restore_point('namelist_smoke_archive_probe'); SELECT pg_switch_wal()")
                failed_wal = ""
                for _ in range(30):
                    failed_wal = run("exec", "-T", "-u", "postgres", "db", "psql", "-d", "postgres", "-Atc", "SELECT last_failed_wal FROM pg_stat_archiver", capture=True).stdout.strip()
                    if failed_wal:
                        break
                    time.sleep(1)
                assert len(failed_wal) == 24, failed_wal
                run("exec", "-T", "db", "test", "-f", f"/var/lib/postgresql/data/pg_wal/{failed_wal}")
                run("exec", "-T", "db", "test", "-f", f"/var/lib/postgresql/data/pg_wal/archive_status/{failed_wal}.ready")
                assert run("exec", "-T", "-u", "postgres", "db", "psql", "-d", "postgres", "-Atc", "SHOW archive_mode", capture=True).stdout.strip() == "on"
            finally:
                run("exec", "-T", "db", "chmod", original_mode, "/var/lib/pgbackrest/smoke")
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
            run("stop", "web", "db", "redis")
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
                        "static_protocol": "gzip+HEAD+304+MIME+404",
                        "browser": "desktop+mobile+admin-editor",
                        "mailpit": "captured",
                        "access_logs": "credential-markers-absent",
                        "encrypted_backup": "full+diff",
                        "configured_repository_failure": "WAL retained; archive_mode=on",
                        "offline_restore": evidence,
                    },
                    indent=2,
                )
            )
        except Exception:
            # This project contains only generated local credentials, never production integrations.
            run("logs", "--no-color", "--tail", "100", "initialize", "db", "web", timeout=30)
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


def browser_smoke(run):
    from playwright.sync_api import expect, sync_playwright

    # An isolated fixture session; no authentication bypass is installed in the application.
    result = run(
        "exec", "-T", "web", "python", "manage.py", "shell", "-c",
        "import json; from core.models import User; from allauth.account.models import EmailAddress; "
        "from django.test import Client; "
        "u=User.objects.create_superuser('smoke-admin', 'smoke@example.invalid', 'Local-smoke-password-429'); "
        "EmailAddress.objects.create(user=u,email=u.email,verified=True,primary=True); "
        "c=Client(); c.force_login(u); print(json.dumps({'session':c.cookies['sessionid'].value}))",
        capture=True,
    )
    cookie = json.loads(result.stdout.strip().splitlines()[-1])["session"]
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if not executable:
        candidates = sorted((Path.home() / "Library/Caches/ms-playwright").glob(
            "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"
        ))
        if candidates:
            executable = str(candidates[-1])
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=executable or None)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            failures = []
            page.on("pageerror", lambda error: failures.append(str(error)))
            page.on("requestfailed", lambda request: failures.append(request.url))
            page.on("response", lambda response: failures.append(response.url) if response.status >= 400 else None)
            origin = "http://localhost:18000"
            page.goto(origin + "/", wait_until="networkidle")
            expect(page.get_by_role("heading", name="最新收录", exact=True)).to_be_visible()
            assert page.locator("body").evaluate("node => getComputedStyle(node).fontFamily")
            page.set_viewport_size({"width": 390, "height": 844})
            page.reload(wait_until="networkidle")
            expect(page.get_by_role("link", name="探索仓库", exact=False)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            context.add_cookies([{"name": "sessionid", "value": cookie, "url": origin, "httpOnly": True}])
            page.goto(origin + "/u/smoke-admin/", wait_until="networkidle")
            assert page.locator(".profile-avatar").evaluate("image => image.complete && image.naturalWidth > 0")
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.goto(origin + "/admin/bulletins/announcement/add/", wait_until="networkidle")
            expect(page.locator(".CodeMirror")).to_be_visible()
            assert page.locator(".editor-toolbar button.preview").is_visible()
            page.locator(".CodeMirror").click()
            page.keyboard.insert_text("**静态资源验收**")
            page.locator(".editor-toolbar button.preview").click()
            expect(page.locator(".editor-preview strong")).to_have_text("静态资源验收")
            assert not failures, failures
            context.close()
        finally:
            browser.close()


if __name__ == "__main__":
    main(configured=False)
    main(configured=True)
