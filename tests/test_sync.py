from unittest.mock import patch

import pytest
from django.utils import timezone

from core.git import GitSnapshot
from core.models import PolicyDocument, SyncRun
from core.runtime import set_setting
from core.tasks import sync_documents, sync_license_policy
from repositories.models import LicensePolicy

pytestmark = pytest.mark.django_db


def test_policy_languages_independent_and_no_announcement_coupling():
    from bulletins.models import Announcement

    set_setting("policy_source_url", "https://github.com/acme/documents.git")
    english = PolicyDocument.objects.create(
        language="en-US", kind="privacy", body="old", source_sha="old-sha"
    )
    files = {
        f"{language}/{kind}.md": f"{language} {kind}"
        for language in ("zh-CN", "en-US")
        for kind in ("privacy", "terms", "inclusion")
    }
    files["en-US/privacy.md"] = "old"
    snapshot = GitSnapshot("a" * 40, timezone.now(), files)
    with patch("core.git.fetch_snapshot", return_value=snapshot):
        assert sync_documents() == snapshot.sha
    english.refresh_from_db()
    assert english.source_sha == "old-sha"
    assert PolicyDocument.objects.count() == 6
    assert not Announcement.objects.exists()


def test_bad_policy_documents_do_not_replace_valid_content():
    set_setting("policy_source_url", "https://github.com/acme/documents.git")
    old = PolicyDocument.objects.create(
        language="zh-CN", kind="privacy", body="valid old policy", source_sha="old"
    )
    with (
        patch("core.git.fetch_snapshot", return_value=GitSnapshot("a" * 40, timezone.now(), {})),
        patch("core.tasks.notify_failure.delay"),
    ):
        with pytest.raises(ValueError):
            sync_documents()
    old.refresh_from_db()
    assert old.body == "valid old policy"
    assert SyncRun.objects.filter(status="failed").exists()


def test_invalid_license_index_preserves_active_policy(tmp_path, settings):
    settings.WORK_ROOT = tmp_path
    set_setting("license_source_url", "https://github.com/acme/license-policy.git")
    old = LicensePolicy.objects.create(
        source_sha="old",
        detector_version="old-detector",
        index_path=str(tmp_path / "old"),
        manifest={},
        active=True,
    )
    with (
        patch(
            "core.git.fetch_snapshot",
            return_value=GitSnapshot("a" * 40, timezone.now(), {"whitelist/file": "text"}),
        ),
        patch("licensecheck.engine.compile_policy", side_effect=ValueError("black white conflict")),
        patch("core.tasks.notify_failure.delay"),
    ):
        with pytest.raises(ValueError):
            sync_license_policy()
    old.refresh_from_db()
    assert old.active
    assert not list(tmp_path.glob("compile-*"))


def test_source_configuration_changed_mid_job_never_activates(tmp_path, settings):
    settings.WORK_ROOT = tmp_path
    set_setting("license_source_url", "https://github.com/acme/one.git")

    def compile_and_change(files, destination):
        set_setting("license_source_url", "https://github.com/acme/two.git")
        return {"detector_version": "test"}

    with (
        patch(
            "core.git.fetch_snapshot",
            return_value=GitSnapshot("a" * 40, timezone.now(), {"whitelist/file": "text"}),
        ),
        patch("licensecheck.engine.compile_policy", side_effect=compile_and_change),
    ):
        assert sync_license_policy() == "superseded"
    assert not LicensePolicy.objects.filter(active=True).exists()


def test_detector_upgrade_recompiles_unchanged_policy_sha(tmp_path, settings):
    settings.WORK_ROOT = tmp_path
    set_setting("license_source_url", "https://github.com/acme/rules.git")
    existing = LicensePolicy.objects.create(
        source_sha="a" * 40,
        detector_version="old",
        index_path=str(tmp_path / "old"),
        manifest={},
        active=True,
    )
    with (
        patch(
            "core.git.fetch_snapshot",
            return_value=GitSnapshot("a" * 40, timezone.now(), {"whitelist/file": "text"}),
        ),
        patch("licensecheck.engine.detector_version", return_value="new"),
        patch(
            "licensecheck.engine.compile_policy", return_value={"detector_version": "new"}
        ) as compile_index,
    ):
        assert sync_license_policy() == "a" * 40
        compile_index.assert_called_once()
    existing.refresh_from_db()
    assert existing.active and existing.detector_version == "new"


def test_missing_cache_after_database_restore_rebuilds_same_version_and_sha(tmp_path, settings):
    from licensecheck.engine import PolicyArtifactError

    settings.WORK_ROOT = tmp_path
    set_setting("license_source_url", "https://github.com/acme/rules.git")
    existing = LicensePolicy.objects.create(
        source_sha="a" * 40,
        detector_version="v1",
        index_path=str(tmp_path / "missing"),
        manifest={},
        active=True,
    )
    with (
        patch(
            "core.git.fetch_snapshot",
            return_value=GitSnapshot("a" * 40, timezone.now(), {"whitelist/file": "text"}),
        ),
        patch("licensecheck.engine.detector_version", return_value="v1"),
        patch(
            "licensecheck.engine.validate_policy_artifacts", side_effect=PolicyArtifactError("missing cache")
        ),
        patch("licensecheck.engine.compile_policy", return_value={"detector_version": "v1"}) as compile_index,
    ):
        assert sync_license_policy() == "a" * 40
        compile_index.assert_called_once()
    existing.refresh_from_db()
    assert existing.index_path != str(tmp_path / "missing")
