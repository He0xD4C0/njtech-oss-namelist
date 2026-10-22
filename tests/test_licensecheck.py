"""Real ScanCode acceptance tests; run in the Linux amd64 scanner image.

No detector mocks, fixtures that pretend to be a ScanCode index, or database are
used. Mac ARM64 Web development may omit the optional scanner group; CI/scanner
validation must explicitly install it and run this marked suite without skips.
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from licensecheck import spdx
from licensecheck.engine import (
    CandidateValidationError,
    PolicyArtifactError,
    PolicyCompilationError,
    compile_policy,
    scan_candidates,
)

pytestmark = pytest.mark.scancode
pytest.importorskip("licensedcode", reason="Install --group scanner in the Linux amd64 scan worker")

MIT = """MIT License

Copyright (c) 2020 Alice Example

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

RESTRICTED = """Example Research License

Copyright (c) 2024 Research Example

You may copy and modify this software solely for personal research purposes.
Commercial use and redistribution are prohibited without separate written
permission from the copyright holder. This software is supplied without any
warranty, and the author accepts no liability for damages arising from use.
"""

CUSTOM = """Example Open Permission

Copyright (c) 2023 Open Example

Every person receiving this work may use, copy, change, publish, redistribute
and sell it for any purpose without payment or other condition. No warranty is
given and no creator accepts liability for any damage arising from this work.
"""


@pytest.fixture(scope="module")
def policy(tmp_path_factory):
    path = tmp_path_factory.mktemp("scancode-policy")
    manifest = compile_policy({
        "whitelist/not-an-spdx-name.weird": MIT,
        "whitelist/custom.anything": CUSTOM,
        "blacklist/mit.txt": RESTRICTED,
        "README.md": "This filename must have no authority.",
    }, path)
    return manifest, path


def scan(policy, files):
    manifest, path = policy
    return scan_candidates(files, manifest, path)


def test_real_compiled_artifacts_and_detector_version(policy):
    manifest, path = policy
    assert manifest["detector_version"].startswith("scancode-toolkit/32.")
    assert (path / "policy-index.pickle").stat().st_size > 0
    assert len(list((path / "rules").glob("*.RULE"))) == 3
    assert json.loads((path / "manifest.json").read_text()) == manifest
    result = scan(policy, {"LICENSE": MIT})
    assert result["decision"] == "published"
    assert "MIT" in result["labels"]
    assert result["evidence"]["files"][0]["policy_matches"]
    assert result["evidence"]["files"][0]["complete_text_coverage"]
    assert all(not item["authorizes"] for item in result["evidence"]["files"][0]["native_matches"])


def test_authors_years_layout_are_equivalent(policy):
    changed = MIT.replace("2020 Alice Example", "2026-2027 Bob Example <bob@example.com>")
    # Change line wrapping, Markdown emphasis and capitalization, not clauses.
    changed = changed.replace("MIT License", "# **MIT LICENSE**").replace("of this software", "of\nthis\tsoftware")
    result = scan(policy, {"LICENCE.md": changed})
    assert result["decision"] == "published"
    assert result["evidence"]["files"][0]["ignored_notices"][0]["line"] == 3


def test_unparsed_placeholder_copyright_holder_requires_review(policy):
    from cluecode.copyrights import (
        CopyrightDetection,
        HolderDetection,
        detect_copyrights_from_lines,
    )

    # Actual ScanCode 32.5 treats "Another" as JUNK and "Author" as AUTH, not
    # as a complete personal name. Never discard the unparsed trailing text.
    header = "Copyright (c) 2026 Another Author"
    notices = list(detect_copyrights_from_lines([(3, header)]))
    assert [notice.copyright for notice in notices if isinstance(notice, CopyrightDetection)] == [
        "Copyright (c) 2026",
    ]
    assert not any(isinstance(notice, HolderDetection) for notice in notices)
    result = scan(policy, {"LICENSE": CUSTOM.replace("2023 Open Example", "2026 Another Author")})
    assert result["decision"] == "needs_review"
    evidence = result["evidence"]["files"][0]
    assert evidence["ignored_notices"] == []
    assert not evidence["complete_text_coverage"]
    assert evidence["authorizing_policies"] == []


@pytest.mark.parametrize("changed", [
    MIT + "\nOnly non-commercial use is permitted.\n",
    MIT.replace("without restriction", "with restrictions"),
    MIT.replace("and/or sell", "and/or not sell"),
    MIT.replace("The above copyright notice and this permission notice shall be included in\n"
                "all copies or substantial portions of the Software.", ""),
    "\n".join(MIT.splitlines()[4:10]),
    MIT.replace("The above copyright notice", "Use only for non-commercial purposes. The above copyright notice"),
])
def test_changed_clauses_fragments_and_added_restrictions_do_not_pass(policy, changed):
    result = scan(policy, {"LICENSE": changed})
    assert result["decision"] == "needs_review"
    assert not result["evidence"]["files"][0]["complete_text_coverage"]


def test_only_policy_grants_eligibility_native_mit_is_not_an_allowlist(tmp_path):
    manifest = compile_policy({"blacklist/only-policy": RESTRICTED}, tmp_path)
    result = scan_candidates({"LICENSE": MIT}, manifest, tmp_path)
    assert "MIT" in result["labels"]
    assert result["decision"] == "needs_review"
    assert not result["evidence"]["files"][0]["authorizing_policies"]


def test_arbitrary_custom_license_is_authorized_by_exact_policy_text(policy):
    result = scan(policy, {"LICENSES/my-text.odd": CUSTOM.replace("2023 Open Example", "2028 Other Example")})
    assert result["decision"] == "published"
    assert result["evidence"]["files"][0]["authorizing_policies"]


def test_blacklist_mixed_and_no_candidates(policy):
    assert scan(policy, {"COPYING": RESTRICTED})["decision"] == "rejected"
    mixed = scan(policy, {"LICENSE": MIT, "LICENSES/restricted": RESTRICTED})
    assert mixed["decision"] == "needs_review"
    assert mixed["evidence"]["reason"] == "mixed_policy_classifications"
    assert scan(policy, {})["decision"] == "rejected"
    assert scan(policy, {"src/LICENSE": MIT})["decision"] == "rejected"
    assert scan(policy, {"LICENSE": "unrecognized licensing terms"})["decision"] == "needs_review"


def test_entire_concatenated_policy_texts_are_required(policy):
    assert scan(policy, {"LICENSE": MIT + "\n" + CUSTOM})["decision"] == "published"
    assert scan(policy, {"LICENSE": MIT + "\n" + RESTRICTED})["decision"] == "needs_review"
    assert scan(policy, {"LICENSE": MIT + "\nExcept commercial uses.\n" + CUSTOM})["decision"] == "needs_review"


@pytest.mark.parametrize("changed", [
    MIT.replace("2020 Alice Example", "2026 Another Author"),
    MIT.replace("2020 Alice Example", "2026 张三 <zhang@example.test>\nCopyright © 2025 开源社区"),
    MIT.replace("Copyright (c) 2020 Alice Example", ""),
    MIT.replace("MIT License", "The MIT License (MIT)"),
    MIT.replace("MIT License", ""),
    MIT.replace("without limitation the rights", "without limitation on the rights"),
    MIT.replace("THE AUTHORS OR COPYRIGHT HOLDERS", "张三和项目贡献者"),
    MIT.replace("Software", "Materials").replace("SOFTWARE IS", "MATERIALS ARE").replace("SOFTWARE", "MATERIALS"),
])
def test_spdx_variable_and_optional_fields_do_not_require_exact_policy_text(policy, changed):
    result = scan(policy, {"LICENSE": changed})
    assert result["decision"] == "published"
    assert result["evidence"]["files"][0]["complete_text_coverage"]


def test_spdx_does_not_interpret_the_contents_of_declared_variables(policy):
    # The matching boundary is SPDX's template, not validation of holder
    # contents. MIT's copyright field is .{0,5000}; its legal meaning is not
    # evaluated here. Fixed text and additions outside these fields stay strict.
    changed = MIT.replace("2020 Alice Example", "2026 Bob Example. Use only for non-commercial purposes.")
    result = scan(policy, {"LICENSE": changed})
    assert result["decision"] == "published"
    assert result["evidence"]["files"][0]["spdx_template_matches"]


@pytest.fixture(scope="module")
def standard_policy(tmp_path_factory):
    path = tmp_path_factory.mktemp("spdx-policy")
    denied = {"SSPL-1.0", "PolyForm-Noncommercial-1.0.0", "PolyForm-Small-Business-1.0.0"}
    texts = {identifier: spdx.templates()[identifier]["text"]
             for identifier in ("MIT", "BSD-3-Clause", "ISC", "Apache-2.0", "AGPL-3.0-only", *sorted(denied))}
    files = {f"{'blacklist' if identifier in denied else 'whitelist'}/{number}": text
             for number, (identifier, text) in enumerate(texts.items())}
    return (compile_policy(files, path), path), texts


@pytest.mark.parametrize("identifier", ["PolyForm-Noncommercial-1.0.0", "PolyForm-Small-Business-1.0.0"])
def test_polyform_official_texts_enable_their_templates(standard_policy, identifier):
    # These official templates contain literal Markdown; stripping it first
    # would reject the official exemplars and disable their variable fields.
    policy, texts = standard_policy
    manifest, _ = policy
    assert any(identifier in record["spdx_templates"] for record in manifest["policies"].values())
    assert scan(policy, {"LICENSE": texts[identifier]})["decision"] == "rejected"


@pytest.mark.parametrize("identifier", ["MIT", "BSD-3-Clause", "ISC", "Apache-2.0", "AGPL-3.0-only"])
def test_official_standard_texts_and_changed_fixed_clauses(standard_policy, identifier):
    policy, texts = standard_policy
    text = texts[identifier]
    assert scan(policy, {"LICENSE": text})["decision"] == "published"
    assert scan(policy, {"LICENSE": text + "\nCommercial use is prohibited.\n"})["decision"] == "needs_review"


def test_bsd_and_isc_organization_fields(standard_policy):
    policy, texts = standard_policy
    bsd = texts["BSD-3-Clause"].replace("<year>", "2026").replace("<owner>", "开源社区")
    bsd = bsd.replace("<organization>", "Example Project").replace("<copyright holder>", "Example Project")
    isc = texts["ISC"].replace("<YEAR>", "2026").replace("<OWNER>", "张三")
    isc = isc.replace("THE AUTHOR", "EXAMPLE PROJECT CONTRIBUTORS")
    assert scan(policy, {"LICENSES/bsd": bsd, "LICENSES/isc": isc})["decision"] == "published"


def test_apache_optional_appendix_and_gnu_https(standard_policy):
    policy, texts = standard_policy
    apache = texts["Apache-2.0"].split("APPENDIX:")[0]
    assert len(apache) < len(texts["Apache-2.0"])
    assert scan(policy, {"LICENSE": apache})["decision"] == "published"
    agpl = texts["AGPL-3.0-only"].replace("http://", "https://")
    assert scan(policy, {"LICENSE": agpl})["decision"] == "published"


def test_template_bundles_preserve_every_policy_class(policy, standard_policy):
    changed = MIT.replace("2020 Alice Example", "2026 Another Author").replace("MIT License", "")
    assert scan(policy, {"LICENSE": changed + "\n" + CUSTOM})["decision"] == "published"
    assert scan(policy, {"LICENSE": RESTRICTED + "\n" + changed})["decision"] == "needs_review"
    assert scan(policy, {"LICENSE": changed + "\n" + RESTRICTED})["decision"] == "needs_review"
    standard, texts = standard_policy
    assert scan(standard, {"LICENSE": texts["SSPL-1.0"]})["decision"] == "rejected"
    mixed = scan(standard, {"LICENSE": changed + "\n" + texts["SSPL-1.0"]})
    assert mixed["decision"] == "needs_review"


def test_native_label_on_modified_policy_does_not_enable_its_template(tmp_path):
    source = MIT.replace("and/or sell", "and/or not sell")
    manifest = compile_policy({"whitelist/mit": source}, tmp_path)
    assert next(iter(manifest["policies"].values()))["spdx_templates"] == []
    assert scan_candidates({"LICENSE": MIT}, manifest, tmp_path)["decision"] == "needs_review"
    assert scan_candidates({"LICENSE": source}, manifest, tmp_path)["decision"] == "published"


def test_template_equivalent_black_white_sources_conflict(tmp_path):
    changed = MIT.replace("MIT License", "").replace("THE AUTHORS OR COPYRIGHT HOLDERS", "OTHER CONTRIBUTORS")
    with pytest.raises(PolicyCompilationError, match="Conflicting"):
        compile_policy({"whitelist/a": MIT, "blacklist/b": changed}, tmp_path)


def test_filename_changes_leave_policy_identity_unchanged(tmp_path):
    first = compile_policy({"whitelist/renameme.license": MIT}, tmp_path / "first")
    second = compile_policy({"whitelist/blacklist-GPL.foo": MIT}, tmp_path / "second")
    assert set(first["policies"]) == set(second["policies"])
    assert first["policies"].values() != second["policies"].values()
    assert scan_candidates({"LICENSE": MIT}, second, tmp_path / "second")["decision"] == "published"


def test_normalized_black_white_conflict_cannot_compile_or_activate(tmp_path):
    with pytest.raises(PolicyCompilationError, match="Conflicting"):
        compile_policy({
            "whitelist/alpha": MIT,
            "blacklist/renamed": MIT.replace("2020 Alice Example", "2030 Another Example"),
        }, tmp_path)
    assert not (tmp_path / "manifest.json").exists()


def test_duplicate_same_classification_compiles_one_rule(tmp_path):
    manifest = compile_policy({"whitelist/a": MIT, "whitelist/b": MIT}, tmp_path)
    assert len(manifest["policies"]) == 1
    assert len(next(iter(manifest["policies"].values()))["sources"]) == 2


def test_atomic_directory_move_leaves_index_usable(tmp_path):
    staging = tmp_path / "staging"
    manifest = compile_policy({"whitelist/a": MIT}, staging)
    active = tmp_path / "active"
    staging.rename(active)
    assert scan_candidates({"LICENSE": MIT}, manifest, active)["decision"] == "published"
    assert str(staging) not in json.dumps(manifest)


def test_invalid_policies_and_active_directory_not_overwritten(tmp_path):
    with pytest.raises(PolicyCompilationError):
        compile_policy({"README.md": MIT}, tmp_path / "empty")
    with pytest.raises(PolicyCompilationError):
        compile_policy({"whitelist/a": ""}, tmp_path / "empty")
    compile_policy({"whitelist/a": MIT}, tmp_path / "valid")
    with pytest.raises(PolicyCompilationError, match="empty staging"):
        compile_policy({"whitelist/a": CUSTOM}, tmp_path / "valid")


def test_corrupt_or_changed_detector_artifact_never_authorizes(tmp_path):
    manifest = compile_policy({"whitelist/a": MIT}, tmp_path)
    incompatible = {**manifest, "detector_version": "other-version"}
    with pytest.raises(PolicyArtifactError, match="version"):
        scan_candidates({"LICENSE": MIT}, incompatible, tmp_path)
    (tmp_path / "policy-index.pickle").write_bytes(b"tampered")
    with pytest.raises(PolicyArtifactError, match="checksum"):
        scan_candidates({"LICENSE": MIT}, manifest, tmp_path)


def test_input_paths_and_binary_text_cannot_escape_artifacts(tmp_path):
    for files in (
        {"../LICENSE": MIT}, {"/LICENSE": MIT}, {"whitelist/a": "binary\x00text"},
        {"LICENSE": "\ud800"}, {"LICENSE": "x" * (2 * 1024 * 1024 + 1)},
        {f"LICENSE-{number}": "text" for number in range(1025)},
    ):
        with pytest.raises(CandidateValidationError):
            compile_policy(files, tmp_path)
        with pytest.raises(CandidateValidationError):
            scan_candidates(files, {}, tmp_path)
    assert not issubclass(PolicyArtifactError, CandidateValidationError)


@pytest.mark.parametrize(("original", "changed"), [
    ("fee $1", "fee €1"),
    ("fee $1", "fee 1"),
    ("condition <1", "condition >1"),
    ("condition < 1", "condition > 1"),
    ("condition ≤1", "condition <1"),
    ("fee 1%", "fee 1"),
    ("condition -1", "condition 1"),
    ("condition 1+1", "condition 1-1"),
    ("condition 1*2", "condition 1/2"),
    ("condition 1 * 2", "condition 1 2"),
    ("condition !=1", "condition =1"),
    ("condition 1:2", "condition 1 2"),
    ("condition [1,2)", "condition (1,2]"),
    ("$1", "- $1"),
])
def test_changed_currency_and_operators_do_not_pass_full_coverage(tmp_path, original, changed):
    source = CUSTOM + "\n" + original + ".\n"
    manifest = compile_policy({"whitelist/policy.txt": source}, tmp_path)
    assert scan_candidates({"LICENSE": source}, manifest, tmp_path)["decision"] == "published"
    result = scan_candidates({"LICENSE": source.replace(original, changed)}, manifest, tmp_path)
    assert result["decision"] == "needs_review"
    assert not result["evidence"]["files"][0]["complete_text_coverage"]


def test_symbols_survive_routine_markdown_layout(tmp_path):
    source = CUSTOM + "\nPermission requires fee $1 and condition <1 plus sum 1+1.\n"
    manifest = compile_policy({"whitelist/policy": source}, tmp_path)
    changed = source.replace("Example Open Permission", "# **Example Open Permission**")
    changed = changed.replace("fee $1", "fee **$1**").replace("condition <1", "condition `<1`")
    changed = changed.replace("sum 1+1", "sum **1** + **1**")
    assert scan_candidates({"LICENSE.md": changed}, manifest, tmp_path)["decision"] == "published"


def test_native_cache_reuses_existing_files_and_serializes_threads(policy, monkeypatch):
    import licensedcode.cache as native
    from scancode_config import licensedcode_cache_dir

    from licensecheck.engine import _native_cache

    index = Path(licensedcode_cache_dir) / native.LICENSE_INDEX_DIR / native.LICENSE_INDEX_FILENAME
    before = index.stat()
    marker = index.parent / "namelist-test-preserve-existing-cache"
    marker.write_text("must survive loads", encoding="utf-8")
    try:
        _native_cache.cache_clear()
        monkeypatch.setattr(native, "_LICENSE_CACHE", None)
        with ThreadPoolExecutor(max_workers=4) as pool:
            loaded = list(pool.map(lambda _: _native_cache(), range(4)))
        assert len({id(cache) for cache in loaded}) == 1
        assert marker.read_text() == "must survive loads"
        after = index.stat()
        assert (before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_ino, after.st_size, after.st_mtime_ns,
        )
    finally:
        marker.unlink(missing_ok=True)


def test_native_shared_cold_cache_is_safe_across_real_processes(tmp_path):
    index_directory = tmp_path / "native-license-cache" / "license_index"
    index_directory.mkdir(parents=True)
    marker = index_directory / "do-not-delete"
    marker.write_text("persistent cache", encoding="utf-8")
    env = {
        **os.environ,
        "SCANCODE_CACHE": str(tmp_path / "runtime-cache"),
        "SCANCODE_LICENSE_INDEX_CACHE": str(index_directory.parent),
        "SCANCODE_PACKAGE_INDEX_CACHE": str(tmp_path / "package-cache"),
    }
    script = """
import json
from pathlib import Path
from licensecheck.engine import _native_evidence
from scancode_config import licensedcode_cache_dir
labels, _ = _native_evidence('MIT License')
stat = (Path(licensedcode_cache_dir) / 'license_index' / 'index_cache').stat()
print(json.dumps({'labels': labels, 'cache': [stat.st_ino, stat.st_size, stat.st_mtime_ns]}))
"""
    processes = [subprocess.Popen(
        [sys.executable, "-c", script], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) for _ in range(2)]
    outputs = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=300)
            assert process.returncode == 0, stderr
            outputs.append(json.loads(stdout.strip().splitlines()[-1]))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert all("MIT" in output["labels"] for output in outputs)
    assert outputs[0]["cache"] == outputs[1]["cache"]
    assert marker.read_text() == "persistent cache"


@pytest.mark.parametrize("candidate", ["gpl", "existing", "custom"])
def test_administrator_preflight_uses_real_scanner_and_complete_text(tmp_path, candidate):
    from licensecheck.review import preflight

    files = {"whitelist/mit.txt": MIT, "blacklist/restricted.txt": RESTRICTED}
    text = {"gpl": spdx.templates()["GPL-3.0-only"]["text"], "existing": MIT, "custom": CUSTOM}[candidate]
    assert preflight(files, "whitelist/reviewed-test.txt", text, tmp_path) == ("allowed" if candidate == "existing" else "add")
    if candidate != "existing":
        manifest = json.loads((tmp_path / "after/manifest.json").read_text())
        result = scan_candidates({"LICENSE": text, "LICENSES/second.txt": RESTRICTED}, manifest, tmp_path / "after")
        assert result["decision"] != "published"


@pytest.mark.parametrize("candidate", [RESTRICTED, CUSTOM + "\n" + RESTRICTED])
def test_administrator_cannot_allow_blacklist_full_text_or_complete_fragment(tmp_path, candidate):
    from licensecheck.review import preflight

    with pytest.raises(PolicyCompilationError):
        preflight({"whitelist/mit.txt": MIT, "blacklist/restricted.txt": RESTRICTED},
                  "whitelist/reviewed-test.txt", candidate, tmp_path)
    assert not (tmp_path / "after").exists()


def test_blacklisted_template_cannot_hide_in_single_line_bundle(tmp_path):
    from licensecheck.review import preflight

    variant = MIT.replace("2020 Alice Example", "2026 Zed-Novel-Author").replace("\n", " ")
    with pytest.raises(PolicyCompilationError):
        preflight({"whitelist/custom.txt": CUSTOM, "blacklist/mit.txt": MIT},
                  "whitelist/reviewed-test.txt", CUSTOM + "\n" + variant + " Additional text.", tmp_path)

