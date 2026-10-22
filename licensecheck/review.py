"""Preflight an explicitly approved exact text without weakening ingestion checks."""

from . import spdx
from .engine import (
    PolicyCompilationError,
    _canonicalize,
    _native_evidence,
    compile_policy,
    scan_candidates,
)


def preflight(files, path, text, destination):
    baseline = compile_policy(files, destination / "before")
    normalized, _ = _canonicalize(text)
    _, native = _native_evidence(text)
    lines = text.splitlines(keepends=True)
    boundaries = sorted({0, len(lines)} | {
        point for match in native for point in (match["start_line"] - 1, match["end_line"])
        if 0 <= point <= len(lines)
    })
    black = [record for record in baseline["policies"].values() if record["classification"] == "blacklist"]
    for record in black:
        if f" {record['normalized_text']} " in f" {normalized} ":
            raise PolicyCompilationError("候选正文包含完整黑名单正文，不能加入允许仓库。")
    templates = {identifier for record in black for identifier in record["spdx_templates"]}
    # A complete native license-text rule can veto an approval, never authorize
    # it. This also catches a blacklisted template embedded on the same line as
    # new prose, where line boundaries cannot isolate its variable fields.
    if any(match["coverage"] == 100 and match["score"] == 100 and match.get("is_license_text")
           and templates.intersection(match.get("labels", [])) for match in native):
        raise PolicyCompilationError("候选正文包含黑名单许可证的完整规则匹配，不能加入允许仓库。")
    for start in boundaries:
        for end in boundaries:
            if end > start and templates and spdx.matching_templates("".join(lines[start:end]), templates):
                raise PolicyCompilationError("候选正文包含完整黑名单模板片段，不能加入允许仓库。")
    if scan_candidates({"LICENSE": text}, baseline, destination / "before")["decision"] == "published":
        return "allowed"
    proposed = compile_policy({**files, path: text}, destination / "after")
    if scan_candidates({"LICENSE": text}, proposed, destination / "after")["decision"] != "published":
        raise PolicyCompilationError("候选正文不能被新策略完整允许。")
    return "add"
