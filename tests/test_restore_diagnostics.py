import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.tasks import restore_diagnostic
from deploy.backup import restore


@pytest.mark.parametrize("phase", ["inventory", "capacity", "restore", "control", "startup", "database", "shutdown", "checksums"])
def test_restore_failure_phase_retained_without_sensitive_public_output(tmp_path, monkeypatch, phase):
    monkeypatch.setattr(restore, "SCRATCH_ROOT", tmp_path)
    monkeypatch.setenv("RESTORE_SCRATCH_ROOT", str(tmp_path))
    monkeypatch.setenv("BACKUP_PASSWORD", "credential-sentinel")
    label = "20261018-031533F_20261022-031532D"
    def command(arguments, **kwargs):
        current = restore.DIAGNOSTIC.get().data["phase"]
        if current == phase:
            raise subprocess.CalledProcessError(42, arguments, stderr="password=credential-sentinel\nDiagnostic detail")
        inventory = [{"backup": [{"label": label, "timestamp": {"stop": 1}, "info": {"size": 1024}}]}]
        return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps(inventory) if current == "inventory" else "", stderr="")
    def database(path):
        if phase == "database":
            raise ValueError("credential-sentinel")
        return {}
    monkeypatch.setattr(restore, "validate_database", database)
    with patch("deploy.backup.restore.subprocess.run", side_effect=command), patch("deploy.backup.restore.shutil.disk_usage", return_value=SimpleNamespace(free=0 if phase == "capacity" else 10**12)):
        with pytest.raises(Exception) as caught:
            restore.restore(SimpleNamespace(backup_set=None, target_time=None, keep=False))
    summary = caught.value.restore_summary
    assert summary["phase"] == phase
    assert "credential-sentinel" not in json.dumps(summary)
    if phase != "inventory":
        assert summary["backup_set"] == label
    retained = tmp_path / "last-restore-result.json"
    assert os.stat(retained).st_mode & 0o777 == 0o600
    if (tmp_path / "last-restore-detail.log").exists():
        assert "credential-sentinel" not in (tmp_path / "last-restore-detail.log").read_text()
    public = restore_diagnostic(json.dumps({"diagnostic": summary}), error=caught.value, elapsed=1.5)
    assert public["phase"] == phase and public["elapsed_seconds"] == 1.5


def test_timeout_and_untrusted_maintenance_output_are_bounded():
    error = subprocess.TimeoutExpired("secret command", 5)
    data = {"diagnostic": {"phase": "startup", "backup_set": "password=secret", "exit_code": 42, "raw": "credential"}}
    safe = restore_diagnostic(json.dumps(data), error=error, elapsed=5)
    assert safe["timed_out"] and safe["phase"] == "startup" and safe["exit_code"] == 42
    assert "backup_set" not in safe and "credential" not in json.dumps(safe)
    assert restore_diagnostic(b"raw password=secret", error=error)["phase"] == "bootstrap"
