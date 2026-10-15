import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def module(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


config = module("deploy/backup/render-config.py", "backup_config")
restore = module("deploy/backup/restore.py", "backup_restore")
manifest = module("deploy/release/manifest.py", "release_manifest")
runtime = module("deploy/release/validate-runtime.py", "release_runtime")


class BackupConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="njtech-ops-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for filename, value in {"cipher": "a" * 48, "s3-key": "test-key", "s3-secret": "test-secret"}.items():
            (self.root / filename).write_text(value)
        self.environment = {
            "BACKUP_REPO_TYPE": "s3",
            "BACKUP_REPO_PATH": "/namelist/test-only",
            "BACKUP_CIPHER_FILE": str(self.root / "cipher"),
            "BACKUP_S3_KEY_FILE": str(self.root / "s3-key"),
            "BACKUP_S3_SECRET_FILE": str(self.root / "s3-secret"),
            "BACKUP_S3_ENDPOINT": "storage.example.invalid",
            "BACKUP_S3_BUCKET": "backups",
            "BACKUP_S3_REGION": "test-region",
        }

    def render(self, **changes):
        with patch.dict(os.environ, self.environment | changes, clear=True):
            return config.configuration()

    def test_cipher_retention_and_wal_policy(self):
        text = self.render()
        for expected in (
            "repo1-cipher-type=aes-256-cbc",
            "repo1-retention-full-type=time",
            "repo1-retention-full=30",
            "repo1-storage-verify-tls=y",
            "archive-async=n",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("archive-push-queue-max", text)
        self.assertNotIn("repo1-retention-archive=", text)
        self.assertNotIn("spool-path=", text)

    def test_line_injection_rejected(self):
        with self.assertRaises(ValueError):
            self.render(BACKUP_S3_BUCKET="bucket\nrepo1-storage-verify-tls=n")

    def test_s3_requires_independent_cipher(self):
        (self.root / "cipher").write_text("short")
        with self.assertRaises(ValueError):
            self.render()

    def test_posix_requires_designated_local_path(self):
        with self.assertRaises(ValueError):
            self.render(BACKUP_REPO_TYPE="posix", BACKUP_REPO_PATH="/etc")
        self.assertIn(
            "repo1-type=posix",
            self.render(BACKUP_REPO_TYPE="posix", BACKUP_REPO_PATH="/var/lib/pgbackrest/test"),
        )

    def test_stanza_and_endpoint_injection_rejected(self):
        for changes in (
            {"BACKUP_STANZA": "[global]"},
            {"BACKUP_S3_ENDPOINT": "https://host/path"},
            {"BACKUP_REPO_PATH": "/"},
            {"BACKUP_REPO_PATH": "/namelist/../production"},
        ):
            with self.assertRaises(ValueError):
                self.render(**changes)


class RestoreIsolationTests(unittest.TestCase):
    def test_live_or_broad_paths_rejected(self):
        for path in ("/", "/var/lib/postgresql/data", "/app", "/tmp"):
            with self.assertRaises(ValueError):
                restore.validate_root(Path(path))

    def test_cleanup_only_fresh_scratch_child(self):
        with tempfile.TemporaryDirectory(prefix="njtech-restore-test-") as directory:
            root = Path(directory)
            data = root / "drill-fresh"
            data.mkdir()
            (data / "result").write_text("temporary")
            restore.remove_scratch(data, root)
            self.assertFalse(data.exists())
            for wrong in (root, root / "production", root.parent / "drill-unrelated"):
                with self.assertRaises(ValueError):
                    restore.remove_scratch(wrong, root)

    def test_symlink_cleanup_rejected(self):
        with tempfile.TemporaryDirectory(prefix="njtech-restore-test-") as directory:
            root = Path(directory)
            other = root / "retained"
            other.mkdir()
            link = root / "drill-link"
            link.symlink_to(other)
            with self.assertRaises(ValueError):
                restore.remove_scratch(link, root)
            self.assertTrue(other.exists())

    def test_raw_validation_does_not_start_application(self):
        text = (ROOT / "deploy/backup/restore.py").read_text()
        self.assertNotIn("django.setup()", text)
        self.assertIn("default_transaction_read_only=on", text)
        self.assertIn('"--archive-mode=off"', text)
        self.assertIn('"pg_checksums"', text)
        self.assertNotIn("--spool-path=", text)
        self.assertNotIn("max_connections = 10", text)


class ReleaseTests(unittest.TestCase):
    def document(self):
        return {
            "source_sha": "a" * 40,
            "images": {
                target: f"ghcr.io/example/namelist-{target}@sha256:{'b' * 64}" for target in manifest.TARGETS
            },
        }

    def test_all_five_immutable_images_required(self):
        document = self.document()
        self.assertEqual(manifest.validate(document), document)
        document["images"].pop("scanner")
        with self.assertRaises(ValueError):
            manifest.validate(document)

    def test_mutable_or_non_ghcr_reference_rejected(self):
        for reference in (
            "ghcr.io/example/web:latest",
            "ghcr.io/example/web@sha256:short",
            f"other.invalid/web@sha256:{'b' * 64}",
        ):
            document = self.document()
            document["images"]["web"] = reference
            with self.assertRaises(ValueError):
                manifest.validate(document)

    def test_no_production_image_rebuild(self):
        text = (ROOT / ".github/workflows/production.yml").read_text()
        self.assertNotIn("build-push-action", text)
        self.assertIn("verify-run", text)
        self.assertIn("environment: production", text)
        self.assertIn("acceptance_reference", text)

    def test_receipt_matches_exact_staged_source_and_digests(self):
        with tempfile.TemporaryDirectory(prefix="njtech-release-test-") as directory:
            source = Path(directory) / "release.json"
            receipt = Path(directory) / "accepted.json"
            document = self.document() | {"ci_run_id": "123"}
            source.write_text(json.dumps(document))
            manifest.main(["accept", str(source), "local-test-evidence", str(receipt)])
            manifest.main(["verify-acceptance", str(source), str(receipt)])
            document["images"]["web"] = f"ghcr.io/example/namelist-web@sha256:{'c' * 64}"
            source.write_text(json.dumps(document))
            with self.assertRaises(ValueError):
                manifest.main(["verify-acceptance", str(source), str(receipt)])

    def test_staging_receipt_checks_current_release_under_deploy_lock(self):
        text = (ROOT / "deploy/release/accept-staging.sh").read_text()
        self.assertLess(text.index("flock -n 9"), text.index("cmp release.json"))


class ComposeBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            result = subprocess.run(
                ["docker", "compose", "--env-file", ".env.example", "config", "--format", "json"],
                cwd=ROOT,
                capture_output=True,
                check=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError:
            raise unittest.SkipTest("Docker Compose CLI is unavailable")
        cls.document = json.loads(result.stdout)

    def test_database_and_broker_not_published(self):
        for name in ("db", "redis"):
            self.assertFalse(self.document["services"][name].get("ports"))
        self.assertTrue(self.document["networks"]["backend"]["internal"])

    def test_deployed_environment_isolation_gate(self):
        from copy import deepcopy

        runtime.validate(self.document, "staging")
        wrong = deepcopy(self.document)
        wrong["services"]["maintenance"]["environment"]["BACKUP_REPO_PATH"] = "/namelist/production"
        with self.assertRaises(ValueError):
            runtime.validate(wrong, "staging")

    def test_deployed_secrets_cannot_point_to_other_environment(self):
        from copy import deepcopy

        wrong = deepcopy(self.document)
        wrong["secrets"]["app_encryption_key"]["file"] = "/srv/namelist/production/secrets/app_encryption_key"
        with self.assertRaises(ValueError):
            runtime.validate(wrong, "staging")

    def test_no_docker_socket_in_any_service(self):
        for service in self.document["services"].values():
            self.assertNotIn("docker.sock", json.dumps(service.get("volumes", [])))

    def test_tmpfs_options_are_not_separate_yaml_entries(self):
        for service in self.document["services"].values():
            for mount in service.get("tmpfs", []):
                self.assertTrue(mount.startswith("/"), mount)

    def test_backup_secrets_only_db_and_maintenance(self):
        for name, service in self.document["services"].items():
            secrets = {item["source"] for item in service.get("secrets", [])}
            if name not in {"db", "maintenance"}:
                self.assertFalse(secrets & {"backup_s3_key", "backup_s3_secret", "backup_cipher"})

    def test_restore_mounts_primary_read_only(self):
        mounts = self.document["services"]["maintenance"]["volumes"]
        data = next(item for item in mounts if item["target"] == "/var/lib/postgresql/data")
        self.assertTrue(data["read_only"])
        self.assertTrue(any(item["target"] == "/var/lib/restore" for item in mounts))

    def test_single_beat_and_scan_concurrency(self):
        self.assertEqual(len([name for name in self.document["services"] if name == "beat"]), 1)
        # .env.example is the staging configuration; the production/default baseline is two.
        self.assertIn("--concurrency=1", self.document["services"]["scanner"]["command"])
        self.assertIn("SCAN_CONCURRENCY:-2", (ROOT / "compose.yaml").read_text())
        self.assertIn("--soft-time-limit=6600", self.document["services"]["scanner"]["command"])
        self.assertIn("--time-limit=7200", self.document["services"]["scanner"]["command"])

    def test_edge_has_no_backend_or_public_ui_route(self):
        nginx = (ROOT / "deploy/edge/nginx.conf").read_text()
        self.assertNotIn("include /etc/nginx/conf.d/", nginx)
        self.assertNotIn("9000", (ROOT / "deploy/edge/site.conf.example").read_text().split("server {")[1])
        self.assertIn("127.0.0.1:9000:9000", (ROOT / "deploy/edge/compose.yaml").read_text())
        self.assertIn("ssl_reject_handshake on", (ROOT / "deploy/edge/00-deny.conf").read_text())

    def test_access_logs_do_not_capture_credentials_in_urls(self):
        import re

        for relative in ("deploy/edge/nginx.conf", "deploy/local-nginx.conf", "deploy/static/nginx.conf"):
            source = (ROOT / relative).read_text()
            value = re.search(r"log_format namelist\s+'([^']+)'", source).group(1)
            self.assertEqual(
                set(re.findall(r"\$[a-z_]+", value)),
                {
                    "$remote_addr",
                    "$request_method",
                    "$status",
                    "$body_bytes_sent",
                    "$request_time",
                },
            )
            self.assertIn("access_log /dev/stdout namelist;", source)
            self.assertNotIn(" combined", source)
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("%(h)s %(m)s %(s)s %(L)s", dockerfile)
        self.assertNotIn("%(r)s", dockerfile)

    def test_local_profile_has_no_server_secrets(self):
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "compose.yaml",
                "-f",
                "deploy/compose.local.yaml",
                "config",
                "--format",
                "json",
            ],
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
            timeout=30,
        )
        document = json.loads(result.stdout)
        for service in document["services"].values():
            self.assertFalse(service.get("secrets"))
        self.assertIn("mailpit", document["services"])
        from cryptography.fernet import Fernet

        Fernet(document["services"]["web"]["environment"]["APP_ENCRYPTION_KEY"].encode())


if __name__ == "__main__":
    unittest.main()
