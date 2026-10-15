import importlib.util
import json
import os
import re
import subprocess
import tarfile
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
manifest = module("scripts/ci_manifest.py", "ci_manifest")
runtime = module("deploy/validate-compose.py", "compose_runtime")


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


class CIArtifactsTests(unittest.TestCase):
    def document(self):
        return {
            "source_sha": "a" * 40,
            "source_tag": "dev-1.0-rc1",
            "ci_run_id": "123",
            "ci_run_attempt": 1,
            "images": {
                target: f"ghcr.io/example/namelist-{target}@sha256:{'b' * 64}" for target in manifest.TARGETS
            },
        }

    def records(self, directory):
        document = self.document()
        for target, reference in document["images"].items():
            record = directory / target / "image.json"
            record.parent.mkdir(parents=True)
            manifest.main([
                "image", target, reference, document["source_sha"], document["source_tag"],
                document["ci_run_id"], str(document["ci_run_attempt"]), str(record),
            ])

    def bundle(self, directory, output):
        document = self.document()
        manifest.main([
            "bundle", str(directory), str(output), document["source_sha"], document["source_tag"],
            document["ci_run_id"], str(document["ci_run_attempt"]),
        ])

    def test_all_four_immutable_images_required(self):
        document = self.document()
        self.assertEqual(set(document["images"]), {"web", "scanner", "maintenance", "postgres"})
        self.assertEqual(manifest.validate(document), document)
        legacy = self.document()
        legacy["images"]["static"] = legacy["images"]["web"]
        with self.assertRaisesRegex(ValueError, "All four"):
            manifest.validate(legacy)
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

    def test_invalid_build_identity_rejected(self):
        for changes in (
            {"source_sha": "wrong"}, {"source_tag": "release-1.0"}, {"source_tag": "main"},
            {"source_tag": "dev-test/nested"}, {"ci_run_id": "0"}, {"ci_run_id": 123},
            {"ci_run_attempt": 0}, {"ci_run_attempt": True}, {"ci_run_attempt": "1"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                manifest.validate(self.document() | changes)

    def test_bundle_contains_exact_manual_inputs_and_matching_manifest(self):
        with tempfile.TemporaryDirectory(prefix="njtech-ci-test-") as directory:
            root = Path(directory)
            self.records(root / "digests")
            output = root / "ci-bundle.tar.gz"
            self.bundle(root / "digests", output)
            with tarfile.open(output) as archive:
                self.assertEqual(set(archive.getnames()), set(manifest.BUNDLE_FILES) | {"manifest.json", "images.env"})
                self.assertTrue(all(entry.isfile() for entry in archive.getmembers()))
                document = json.load(archive.extractfile("manifest.json"))
                self.assertEqual(document, self.document())
                exported = archive.extractfile("images.env").read().decode()
                self.assertEqual(exported, manifest.environment(document))
                archive.extractall(root / "manual", filter="data")
            regenerated = root / "images.env"
            manifest.main(["env", str(root / "manual/manifest.json"), str(regenerated)])
            self.assertEqual(regenerated.read_text(), exported)

    def test_missing_image_record_rejected_before_packaging(self):
        with tempfile.TemporaryDirectory(prefix="njtech-ci-test-") as directory:
            root = Path(directory)
            self.records(root / "digests")
            (root / "digests/scanner/image.json").unlink()
            output = root / "ci-bundle.tar.gz"
            with self.assertRaisesRegex(ValueError, "All four image digests are required"):
                self.bundle(root / "digests", output)
            self.assertFalse(output.exists())

    def test_duplicate_unknown_and_mismatched_records_rejected(self):
        for changes, error in (
            ({}, "duplicate image target"),
            ({"target": "unexpected"}, "Unknown"),
            ({"source_sha": "c" * 40}, "Mismatched build identity"),
            ({"source_tag": "dev-other"}, "Mismatched build identity"),
            ({"ci_run_id": "456"}, "Mismatched build identity"),
            ({"ci_run_attempt": 2}, "Mismatched build identity"),
            ({"ci_run_attempt": True}, "Invalid CI run attempt"),
            ({"ci_run_attempt": 1.0}, "Invalid CI run attempt"),
        ):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory(prefix="njtech-ci-test-") as directory:
                root = Path(directory)
                self.records(root / "digests")
                record = json.loads((root / "digests/web/image.json").read_text()) | changes
                extra = root / "digests/extra/image.json"
                extra.parent.mkdir()
                extra.write_text(json.dumps(record))
                with self.assertRaisesRegex(ValueError, error):
                    self.bundle(root / "digests", root / "ci-bundle.tar.gz")

    def test_unlisted_credentials_caches_and_scripts_are_not_packaged(self):
        with tempfile.TemporaryDirectory(prefix="njtech-ci-test-") as directory:
            root = Path(directory)
            self.records(root / "digests")
            source = (root / "source").resolve()
            for relative in manifest.BUNDLE_FILES:
                filename = source / relative
                filename.parent.mkdir(parents=True, exist_ok=True)
                filename.write_text("non-sensitive example\n")
            for relative in (".env", "secrets/password", "deploy/backup/.env", "deploy/backup/__pycache__/cache.pyc", "deploy/release/deploy.sh"):
                filename = source / relative
                filename.parent.mkdir(parents=True, exist_ok=True)
                filename.write_text("test-only-secret-marker")
            output = root / "ci-bundle.tar.gz"
            with patch.object(manifest, "ROOT", source):
                self.bundle(root / "digests", output)
            with tarfile.open(output) as archive:
                for entry in archive.getmembers():
                    self.assertNotIn(b"test-only-secret-marker", archive.extractfile(entry).read())

    def test_manual_bundle_loads_all_four_image_digests(self):
        with tempfile.TemporaryDirectory(prefix="njtech-ci-test-") as directory:
            root = Path(directory)
            self.records(root / "digests")
            output = root / "ci-bundle.tar.gz"
            self.bundle(root / "digests", output)
            with tarfile.open(output) as archive:
                archive.extractall(root / "manual", filter="data")
            variables = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", (ROOT / "compose.yaml").read_text()))
            environment = {
                key: value for key, value in os.environ.items()
                if key not in variables and not key.startswith("COMPOSE_")
            }
            try:
                result = subprocess.run(
                    ["docker", "compose", "--env-file", ".env.example", "--env-file", "images.env", "-f", "compose.yaml", "config", "--format", "json"],
                    cwd=root / "manual", env=environment, capture_output=True, check=True, text=True, timeout=30,
                )
            except FileNotFoundError:
                self.skipTest("Docker Compose CLI is unavailable")
            document = json.loads(result.stdout)
            for target, reference in self.document()["images"].items():
                service = "db" if target == "postgres" else target
                self.assertEqual(document["services"][service]["image"], reference)
            runtime.validate(document, "staging")

    def test_only_dev_tag_workflow_can_start_ci(self):
        workflows = ROOT / ".github/workflows"
        self.assertEqual(
            {path.name for path in workflows.iterdir() if path.suffix in {".yml", ".yaml"}},
            {"checks.yml", "release.yml"},
        )
        text = (workflows / "release.yml").read_text()
        trigger = re.search(r"(?ms)^on:\n(.*?)(?=^permissions:)", text).group(1)
        self.assertEqual(trigger.strip(), "push:\n    tags: ['dev-*']")
        checks = (workflows / "checks.yml").read_text()
        self.assertEqual(re.search(r"(?ms)^on:\n(.*?)(?=^permissions:)", checks).group(1).strip(), "workflow_call:")
        jobs = re.findall(r"^  ([a-z][a-z0-9-]*):$", text.split("jobs:\n", 1)[1], re.MULTILINE)
        self.assertEqual(jobs, ["checks", "images", "bundle"])
        self.assertIn("    needs: checks\n", text)
        self.assertIn("    needs: images\n", text)
        self.assertIn("matrix: {target: [web, scanner, maintenance, postgres]}", text)
        for source in (text, checks):
            self.assertNotRegex(source, r"(?m)workflow_dispatch|pull_request|DEPLOY_|secrets\.|^    environment:")
        self.assertIn(":ci-${{ github.run_id }}-${{ github.run_attempt }}", text)
        self.assertIn("pattern: 'image-*-${{ github.run_attempt }}'", text)
        self.assertIn("name: ci-bundle, path: ci-bundle.tar.gz, retention-days: 90", text)
        self.assertNotIn(":latest", text)
        self.assertFalse((ROOT / "deploy/release").exists())


class ComposeBoundaryTests(unittest.TestCase):
    @staticmethod
    def render_compose(*, local=False):
        files = ("compose.yaml", "deploy/compose.local.yaml")
        variables = {
            variable
            for filename in files
            for variable in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", (ROOT / filename).read_text())
        }
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in variables and not key.startswith("COMPOSE_")
        }
        command = ["docker", "compose", "--env-file", os.devnull if local else ".env.example"]
        for filename in files if local else files[:1]:
            command.extend(["-f", filename])
        command.extend(["config", "--format", "json"])
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            check=True,
            text=True,
            timeout=30,
        )
        return json.loads(result.stdout)

    @classmethod
    def setUpClass(cls):
        try:
            cls.document = cls.render_compose()
        except FileNotFoundError:
            raise unittest.SkipTest("Docker Compose CLI is unavailable")

    def test_host_configuration_cannot_override_compose_inputs(self):
        expected_local = self.render_compose(local=True)
        overrides = {
            "DJANGO_DEBUG": "1",
            "COMPOSE_PROJECT_NAME": "namelist-production",
            "SITE_ENVIRONMENT": "production",
            "SECRET_DIR": "/srv/namelist/production/secrets",
            "BACKUP_REPO_PATH": "/namelist/production",
            "SCAN_CONCURRENCY": "99",
            "APP_PORT": "9999",
            "TRUST_PROXY_HEADERS": "1",
            "DB_MEMORY": "1m",
            "COMPOSE_FILE": "missing-compose.yaml",
            "COMPOSE_ENV_FILES": "missing-env-file",
            "COMPOSE_PROFILES": "ops",
        }
        with patch.dict(os.environ, overrides):
            before = dict(os.environ)
            self.assertEqual(self.render_compose(), self.document)
            self.assertEqual(self.render_compose(local=True), expected_local)
            self.assertEqual(dict(os.environ), before)

    def test_compose_commands_preserve_tool_environment_and_explicit_inputs(self):
        tools = {
            "PATH": os.environ.get("PATH", ""),
            "DOCKER_CONTEXT": "test-context",
            "DOCKER_HOST": "unix:///test-only/docker.sock",
            "DOCKER_CONFIG": "/test-only/docker-config",
        }
        with patch.dict(os.environ, tools), patch(
            "subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="{}")
        ) as run:
            before = dict(os.environ)
            for local in (False, True):
                with self.subTest(local=local):
                    self.render_compose(local=local)
                    expected = ["docker", "compose", "--env-file", os.devnull if local else ".env.example"]
                    expected.extend(["-f", "compose.yaml"])
                    if local:
                        expected.extend(["-f", "deploy/compose.local.yaml"])
                    expected.extend(["config", "--format", "json"])
                    self.assertEqual(run.call_args.args[0], expected)
                    self.assertEqual(run.call_args.kwargs["cwd"], ROOT)
                    for key, value in tools.items():
                        self.assertEqual(run.call_args.kwargs["env"][key], value)
                    self.assertEqual(dict(os.environ), before)

    def test_database_and_broker_not_published(self):
        for name in ("db", "redis"):
            self.assertFalse(self.document["services"][name].get("ports"))
        self.assertTrue(self.document["networks"]["backend"]["internal"])

    def test_deployed_environment_isolation_gate(self):
        from copy import deepcopy

        runtime.validate(self.document, "staging")
        wrong = deepcopy(self.document)
        wrong["services"]["maintenance"]["environment"]["BACKUP_REPO_PATH"] = "/namelist/production"
        with self.assertRaisesRegex(ValueError, "Backup prefix belongs to another environment"):
            runtime.validate(wrong, "staging")

    def test_debug_is_rejected_in_deployed_environment(self):
        from copy import deepcopy

        wrong = deepcopy(self.document)
        wrong["services"]["web"]["environment"]["DJANGO_DEBUG"] = "1"
        with self.assertRaisesRegex(ValueError, "Debug is not allowed in a deployed environment"):
            runtime.validate(wrong, "staging")

    def test_deployed_secrets_cannot_point_to_other_environment(self):
        from copy import deepcopy

        wrong = deepcopy(self.document)
        wrong["secrets"]["app_encryption_key"]["file"] = "/srv/namelist/production/secrets/app_encryption_key"
        with self.assertRaisesRegex(ValueError, "Server secrets must be isolated"):
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

    def test_proxy_services_images_and_bundle_inputs_absent(self):
        local = self.render_compose(local=True)
        for document in (self.document, local):
            self.assertNotIn("static", document["services"])
            self.assertNotIn("localproxy", document["services"])
            self.assertNotIn("frontend", document["networks"])
            self.assertEqual(document["services"]["web"]["environment"]["TRUST_PROXY_HEADERS"], "0")
            self.assertEqual(set(document["services"]["web"]["networks"]), {"backend", "egress"})
        for relative in ("deploy/edge/compose.yaml", "deploy/static/nginx.conf", "deploy/local-nginx.conf", "deploy/tests/edge_smoke.py"):
            self.assertFalse((ROOT / relative).exists())
        self.assertNotIn("NGINX_IMAGE", (ROOT / "Dockerfile").read_text())
        self.assertFalse(any("nginx" in name or "edge/" in name for name in manifest.BUNDLE_FILES))

    def test_only_one_loopback_web_port(self):
        for document in (self.document, self.render_compose(local=True)):
            ports = document["services"]["web"]["ports"]
            self.assertEqual(len(ports), 1)
            self.assertEqual(ports[0]["host_ip"], "127.0.0.1")
            self.assertEqual(ports[0]["target"], 8000)
            self.assertEqual(ports[0]["published"], "18000")

    def test_invalid_web_or_data_port_publication_rejected(self):
        from copy import deepcopy

        for changes in ({"host_ip": "0.0.0.0"}, {"host_ip": "::"}, {"host_ip": None}, {"target": 8080}, {"protocol": "udp"}, {"published": "0"}, {"published": "65536"}, {"published": "18000-18001"}):
            wrong = deepcopy(self.document)
            wrong["services"]["web"]["ports"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "loopback port targeting 8000"):
                runtime.validate(wrong, "staging")
        for ports in ([], self.document["services"]["web"]["ports"] * 2):
            wrong = deepcopy(self.document)
            wrong["services"]["web"]["ports"] = ports
            with self.assertRaisesRegex(ValueError, "exactly one loopback"):
                runtime.validate(wrong, "staging")
        for name in ("db", "redis", "worker", "scanner", "beat", "maintenance", "initialize"):
            wrong = deepcopy(self.document)
            wrong["services"][name]["ports"] = [{"target": 5432, "published": "5432"}]
            with self.assertRaisesRegex(ValueError, "Private data/worker ports"):
                runtime.validate(wrong, "staging")
        wrong = deepcopy(self.document)
        wrong["services"]["web"]["network_mode"] = "host"
        with self.assertRaisesRegex(ValueError, "loopback port targeting 8000"):
            runtime.validate(wrong, "staging")

    def test_access_logs_do_not_capture_credentials_in_urls(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("%(h)s %(m)s %(s)s %(L)s", dockerfile)
        self.assertNotIn("%(r)s", dockerfile)

    def test_local_profile_has_no_server_secrets(self):
        document = self.render_compose(local=True)
        for service in document["services"].values():
            self.assertFalse(service.get("secrets"))
        self.assertIn("mailpit", document["services"])
        self.assertIn("egress", document["services"]["mailpit"]["networks"])
        from cryptography.fernet import Fernet

        Fernet(document["services"]["web"]["environment"]["APP_ENCRYPTION_KEY"].encode())


if __name__ == "__main__":
    unittest.main()
