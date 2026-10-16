"""Compile public policy texts and conservatively scan root license candidates.

The native ScanCode database is identification evidence, never an allowlist. A
separate, real ScanCode LicenseIndex contains only policy texts. Exact matches
cover the complete normalized candidate. Official SPDX templates additionally
allow their declared variable/optional fields, only after the policy text itself
fully matches that template. Approximate/native matches cannot grant admission.

Index pickles are generated locally, not fetched from Git. They must stay in the
server-owned WORK_ROOT: never accept an uploaded/precompiled index. Their checksum,
manifest and detector version are checked before loading. No paths inside an
artifact depend on the staging directory, so callers can atomically rename it.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import re
import unicodedata
from functools import lru_cache
from importlib import metadata
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any

from . import spdx

FORMAT_VERSION = 1
ENGINE_VERSION = "policy-text-v3"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_FILES = 1024
_NATIVE_CACHE_LOCK = RLock()


class ScannerUnavailable(RuntimeError):
    """The optional real ScanCode runtime or a native library is unavailable."""


class CandidateValidationError(ValueError):
    """Deterministically invalid source input, distinct from scanner/cache faults."""


class PolicyCompilationError(ValueError):
    """Invalid/ambiguous policy: the caller must retain the last active index."""


class PolicyArtifactError(ValueError):
    """Missing, corrupted or incompatible compiled artifacts; never auto-publish."""


def _sha(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _detector_version() -> str:
    try:
        return (f"scancode-toolkit/{metadata.version('scancode-toolkit')}; {ENGINE_VERSION}; "
                f"SPDX-tools/{spdx.TOOLS_VERSION}; SPDX/{spdx.DATA_VERSION}; "
                f"JPype/{metadata.version('jpype1')}")
    except metadata.PackageNotFoundError as exc:
        raise ScannerUnavailable("Install the locked scanner dependency group on a scan worker") from exc


def detector_version() -> str:
    """Return the installed ScanCode/engine version without importing the scanner."""
    return _detector_version()


@lru_cache(maxsize=1)
def _native_cache():
    """Load/reuse builtin identification data without destructive reindex flags.

    ScanCode 32.5's only_builtin=True deletes license_index BEFORE its own
    lock, even on a cold cache. Its default fast-path also reads outside that
    lock. Serialize our complete load/build across threads and worker processes
    so a reader cannot race a writer's non-atomic pickle dump.
    """
    _runtime()
    from scancode import lockfile
    from scancode_config import scancode_cache_dir

    with _NATIVE_CACHE_LOCK:
        lock_path = Path(scancode_cache_dir) / "namelist-native-identification.lock"
        with lockfile.FileLock(str(lock_path)).locked(timeout=600):
            cache = _runtime()["native_cache"]()
            if cache.additional_license_directory or cache.additional_license_plugins:
                raise ScannerUnavailable("Native identification requires a dedicated cache without additional rules")
            return cache


@lru_cache(maxsize=1)
def _runtime() -> dict[str, Any]:
    # Lazy imports keep Web and general workers independent of ScanCode binaries.
    try:
        from cluecode.copyrights import detect_copyrights_from_lines
        from licensedcode.cache import get_cache
        from licensedcode.index import LicenseIndex
        from licensedcode.models import Rule
        from licensedcode.tokenize import index_tokenizer, query_pattern

        spdx.templates()
        spdx._comparator()
    except Exception:  # noqa: BLE001 - native loader uses generic, environment-bearing exceptions
        # Some ScanCode native-library import errors include os.environ in their
        # exception text. Never chain that text into task/log error reporting.
        raise ScannerUnavailable("ScanCode/SPDX runtime or offline templates are not available in this worker") from None
    return {
        "copyrights": detect_copyrights_from_lines,
        "index": LicenseIndex,
        "rule": Rule,
        "tokens": index_tokenizer,
        "word_spans": re.compile(query_pattern).finditer,
        "native_cache": get_cache,
    }


def _validate_files(files: dict[str, str]) -> None:
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        raise CandidateValidationError("License files must be a bounded filename/text mapping")
    total = 0
    for filename, text in files.items():
        if not isinstance(filename, str) or not isinstance(text, str):
            raise TypeError("License filenames and contents must be text")
        path = PurePosixPath(filename)
        if (
            not filename or "\\" in filename or "\x00" in filename or path.is_absolute()
            or any(part in {"", ".", ".."} for part in filename.split("/"))
        ):
            raise CandidateValidationError("Unsafe license file path")
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            raise CandidateValidationError("License text is not valid UTF-8") from None
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or "\x00" in text:
            raise CandidateValidationError("License text exceeds scan limits or contains binary data")


def _tokens(text: str) -> list[str]:
    # Preserve EVERY word, including stopwords and numbers. In particular,
    # removing 'not', 'only', or a monetary amount must invalidate equivalence.
    return list(_runtime()["tokens"](text, stopwords=frozenset(), preserve_case=False))


def _coverage_tokens(text: str) -> list[str]:
    """Retain semantic symbols alongside ScanCode's words, in source order.

    This is a conservative coverage guard, not a second license detector. A
    small formatting allowlist handles ordinary Markdown; currency, arithmetic,
    comparison and percentage signs must never disappear in the word tokenizer.
    """
    text = text.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"}))
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"^\s{0,3}(?:\*\s*){3,}$|^\s{0,3}(?:-\s*){3,}$|^\s{0,3}[_=`~]{3,}\s*$", "", text)
    # Do not erase leading '-'/'+' as list bullets: '- $1' or '+ limit' can be
    # a substantive unary operator in a custom license. Ambiguity goes to review.
    # Paired emphasis/inline-code delimiters only. An unpaired '*' or arithmetic
    # '2 * 3'/'2*3' remains significant rather than being erased as Markdown.
    text = re.sub(r"(?<!\w)(\*\*|__|\*|_|`)(?=\S)(.+?)(?<!\s)\1(?!\w)", r"\2", text)
    tokens = []
    cursor = 0
    for match in _runtime()["word_spans"](text):
        tokens.extend(char for char in text[cursor:match.start()]
                      if unicodedata.category(char).startswith(("S", "P")))
        # ScanCode keeps '+' inside words (for GPL-2.0+). Split it only for the
        # coverage guard so spacing around an arithmetic plus is immaterial.
        for part in re.split(r"(\+)", match.group()):
            tokens.extend([part] if part == "+" else _tokens(part))
        cursor = match.end()
    tokens.extend(char for char in text[cursor:]
                  if unicodedata.category(char).startswith(("S", "P")))
    return tokens


# Copyright parsing can sometimes include trailing prose in the holder. Such
# lines cannot be erased wholesale: false negatives belong in the review queue.
_CLAUSE_WORDS = frozenset({
    "permission", "permitted", "may", "must", "shall", "only", "not", "commercial",
    "noncommercial", "restriction", "restrictions", "provided", "without", "except",
    "subject", "license", "licence", "redistribution", "distribute", "distribution",
    "use", "using", "warranty", "liability", "condition", "conditions",
    "ban", "banned", "prohibit", "prohibited", "require", "required", "forbidden",
    "payment", "fee", "pay", "patent",
})


def _canonicalize(text: str) -> tuple[str, list[dict]]:
    """Ignore only isolated, fully parsed copyright holder/year notices.

    This is NOT license matching. Real ScanCode parses the notices and later
    matches the retained text. Substantive prose on the same line is retained;
    ordinary copyright-clause paragraphs and 'All rights reserved' are retained.
    """
    text = unicodedata.normalize("NFC", text.removeprefix("\ufeff"))
    retained = []
    ignored = []
    for number, line in enumerate(text.splitlines(), 1):
        notice = line.replace("©", "Copyright ")
        words = _tokens(notice)
        if words and words[0] == "copyright" and not set(words[1:]).intersection(_CLAUSE_WORDS):
            detections = list(_runtime()["copyrights"](
                numbered_lines=[(number, notice)], include_holders=False, include_authors=False,
                include_copyright_allrights=False,
            ))
            for detection in detections:
                if detection.start_line != number or detection.end_line != number:
                    continue
                detected = _tokens(detection.copyright)
                # ScanCode normalizes the optional (c) marker in notices.
                actual = words[1:] if words[:2] != ["copyright", "c"] else words[2:]
                parsed = detected[1:] if detected[:2] != ["copyright", "c"] else detected[2:]
                suffix = ["all", "rights", "reserved"]
                # ScanCode's grammar classifies the conventional placeholder
                # surname "Example" as a common noun, sometimes leaving it out
                # of an otherwise complete notice. No arbitrary trailing prose
                # is accepted: only this placeholder may extend the parsed name.
                variants = (parsed, parsed + ["example"])
                if any(actual == variant or actual == variant + suffix for variant in variants):
                    ignored.append({"line": number, "text": line, "detector": "ScanCode copyright parser"})
                    if actual[-3:] == suffix:
                        clause = re.search(r"\ball\s+rights\s+reserved\b.*$", line, re.IGNORECASE)
                        retained.extend(_coverage_tokens(clause.group()) if clause else suffix)
                    break
            else:
                retained.extend(_coverage_tokens(line))
            continue
        retained.extend(_coverage_tokens(line))
    return " ".join(retained), ignored


def _native_evidence(text: str) -> tuple[list[str], list[dict]]:
    cache = _native_cache()
    matches = cache.index.match(query_string=text)
    labels = set()
    evidence = []
    for match in matches:
        expression = match.rule.license_expression
        if not expression:
            continue
        for key in match.rule.licensing.license_keys(expression):
            license_ = cache.db.get(key)
            labels.add(license_.spdx_license_key if license_ and license_.spdx_license_key else key)
        evidence.append({
            "expression": expression, "rule": match.rule.identifier,
            "matcher": match.matcher, "score": match.score(), "coverage": match.coverage(),
            "start_line": match.start_line, "end_line": match.end_line,
            "authorizes": False,
        })
    return sorted(labels), evidence


def compile_policy(files: dict[str, str], destination: Path) -> dict:
    """Build a versioned, relocatable ScanCode policy index in an EMPTY directory.

    Paths below whitelist/ and blacklist/ select classification only. Their
    names/extensions never define license identity. Contradictory normalized
    texts raise PolicyCompilationError before writing/activation.
    """
    _validate_files(files)
    runtime = _runtime()
    version = _detector_version()
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise PolicyCompilationError("Compile into a new empty staging directory, never an active index")
    records: dict[str, dict] = {}
    conflict_classes: dict[str, str] = {}
    template_classes: dict[str, str] = {}
    for filename, text in sorted(files.items()):
        parts = PurePosixPath(filename).parts
        if len(parts) < 2 or parts[0] not in {"whitelist", "blacklist"}:
            continue
        normalized, ignored = _canonicalize(text)
        if not normalized:
            raise PolicyCompilationError(f"Policy file has no license text: {filename}")
        labels, native = _native_evidence(text)
        template_ids = spdx.matching_templates(text, labels)
        if not template_ids:
            # Native labels are only a shortlist, not a recognition threshold.
            # Official source texts provide a cheap second shortlist, avoiding
            # hundreds of long comparisons when ScanCode uses a different ID.
            sources = [identifier for identifier, data in spdx.templates().items() if data["text"] == text]
            template_ids = spdx.matching_templates(text, sources or spdx.templates())
        identity = "SPDX:" + ",".join(template_ids) if template_ids else normalized
        key = f"policy-{_sha(identity)}"
        classification = parts[0]
        for identifier in template_ids:
            if identifier in template_classes and template_classes[identifier] != classification:
                raise PolicyCompilationError(f"Conflicting whitelist/blacklist SPDX template: {identifier}")
            template_classes[identifier] = classification
        # For rejecting ambiguous policy conflicts ONLY, err on the side of
        # assuming isolated copyright headers are variable. This broader check
        # never authorizes a candidate: full exact/template coverage below
        # still governs publication. It catches unparsed pseudonyms
        # that could otherwise hide the same clauses in both policy classes.
        conflict_text = " ".join(
            " ".join(_coverage_tokens(line))
            for line in unicodedata.normalize("NFC", text).splitlines()
            if (words := _tokens(line.replace("©", "Copyright ")))
            and not (words[0] == "copyright" and not set(words[1:]).intersection(_CLAUSE_WORDS))
        )
        fingerprint = _sha(conflict_text)
        if fingerprint in conflict_classes and conflict_classes[fingerprint] != classification:
            raise PolicyCompilationError(f"Conflicting whitelist/blacklist license clauses: {key}")
        conflict_classes[fingerprint] = classification
        if key in records:
            record = records[key]
            if record["classification"] != classification:
                raise PolicyCompilationError(f"Conflicting whitelist/blacklist text: {key}")
            record["sources"].append({"path": filename, "sha256": _sha(text), "ignored_notices": ignored})
            continue
        records[key] = {
            "id": key, "classification": classification, "normalized_text": normalized,
            "spdx_templates": template_ids,
            "labels": template_ids or labels or [f"LicenseRef-{key}"], "native_matches": native,
            "sources": [{"path": filename, "sha256": _sha(text), "ignored_notices": ignored}],
        }
    if not records:
        raise PolicyCompilationError("Policy has no whitelist/blacklist license texts")

    destination.mkdir(parents=True, exist_ok=True)
    rules_dir = destination / "rules"
    rules_dir.mkdir()
    rules = []
    for key, record in sorted(records.items()):
        filename = rules_dir / f"{key}.RULE"
        filename.write_text(
            "---\n" + f"license_expression: {key}\n"
            "is_license_text: true\nis_builtin: false\nminimum_coverage: 100\n"
            "relevance: 100\nis_continuous: true\n---\n" + record["normalized_text"] + "\n",
            encoding="utf-8",
        )
        rules.append(runtime["rule"].from_file(str(filename), is_builtin=False))
    try:
        index = runtime["index"](rules=rules, _all_languages=True)
    except Exception:  # noqa: BLE001 - ScanCode raises generic errors for rule-token collisions
        raise PolicyCompilationError("ScanCode could not compile these policy texts; keep the active index") from None
    # Exercise every generated rule against the actual index before it can be
    # activated, catching unsupported/weak rules or duplicate ScanCode tokens.
    for key, record in records.items():
        found = index.match(query_string=record["normalized_text"], approximate=False)
        if not any(m.rule.license_expression == key and m.coverage() == 100 for m in found):
            raise PolicyCompilationError(f"Generated ScanCode rule failed self-test: {key}")
    payload = pickle.dumps(index, protocol=pickle.HIGHEST_PROTOCOL)
    (destination / "policy-index.pickle").write_bytes(payload)
    manifest = {
        "format_version": FORMAT_VERSION, "detector_version": version,
        "index_sha256": _sha(payload), "policies": records,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8",
    )
    return manifest


def _load_policy(manifest: dict, index_path: Path):
    if manifest.get("format_version") != FORMAT_VERSION or manifest.get("detector_version") != _detector_version():
        raise PolicyArtifactError("Policy index version differs from this detector; recompile before use")
    index_path = Path(index_path)
    manifest_file = index_path / "manifest.json"
    index_file = index_path / "policy-index.pickle"
    if manifest_file.is_symlink() or index_file.is_symlink():
        raise PolicyArtifactError("Compiled artifacts must be local regular files")
    try:
        stored = json.loads(manifest_file.read_text(encoding="utf-8"))
        payload = index_file.read_bytes()
    except (OSError, ValueError) as exc:
        raise PolicyArtifactError("Compiled artifacts are missing or unreadable") from exc
    if stored != manifest or _sha(payload) != manifest.get("index_sha256"):
        raise PolicyArtifactError("Compiled artifact checksum/manifest mismatch")
    try:
        index = pickle.loads(payload)
    except Exception as exc:
        raise PolicyArtifactError("Compiled ScanCode index cannot be loaded") from exc
    if not isinstance(index, _runtime()["index"]):
        raise PolicyArtifactError("Compiled artifact is not a ScanCode LicenseIndex")
    return index


def validate_policy_artifacts(manifest: dict, index_path: Path) -> None:
    """Validate the local cache before an unchanged-source synchronization skip."""
    _load_policy(manifest, index_path)


def _complete_sequence(text: str, eligible: set[str], records: dict) -> list[str] | None:
    """Require exact ALL-word/symbol coverage, in addition to real ScanCode matches.

    A file may concatenate complete policies; intervening/new/deleted clauses,
    including words unknown to ScanCode's index, are never discarded.
    """
    words = text.split()
    reachable: dict[int, list[str]] = {0: []}
    for start in range(len(words)):
        if start not in reachable:
            continue
        for key in sorted(eligible):
            policy = records[key]["normalized_text"].split()
            end = start + len(policy)
            if words[start:end] == policy and end not in reachable:
                reachable[end] = reachable[start] + [key]
    return reachable.get(len(words)) if words else None


def _is_candidate(filename: str) -> bool:
    parts = PurePosixPath(filename).parts
    if len(parts) == 2 and parts[0].upper() == "LICENSES":
        return True
    if len(parts) != 1:
        return False
    name = parts[0].lower()
    return any(name == base or name.startswith((base + ".", base + "-", base + "_"))
               for base in ("license", "licence", "copying", "unlicense", "copyright"))


def _template_sequence(
    text: str, native: list[dict], native_labels: list[str], index, records: dict,
) -> tuple[list[str] | None, list]:
    """Cover the entire file with full policy/template matches, including bundles.

    Native match boundaries only suggest partitions; they never authorize text.
    Every character belongs to a fully checked segment, with no skipped gaps.
    Keep all successful policy classes so an overlapping blacklist cannot lose
    to the first whitelist path through the file.
    """
    lines = text.splitlines(keepends=True)
    ends = sorted({len(lines)} | {
        point for match in native for point in (match["start_line"] - 1, match["end_line"])
        if 0 < point <= len(lines)
    })
    templates = {}
    for key, record in records.items():
        for identifier in record["spdx_templates"]:
            templates.setdefault(identifier, set()).add(key)
    if not templates:
        return None, []
    hints = set(native_labels).intersection(templates)
    reachable = {0: (set(), [])}
    for start in [0, *ends]:
        if start not in reachable:
            continue
        for end in reversed(ends):
            if end <= start:
                continue
            segment = "".join(lines[start:end])
            identifiers = spdx.matching_templates(segment, hints)
            remaining = set(templates) - hints
            if identifiers:
                matched_classes = {records[key]["classification"]
                                   for identifier in identifiers for key in templates[identifier]}
                # One full match proves this class. Still check every template
                # of the other class, so a native shortlist cannot hide a mix.
                remaining = {identifier for identifier in remaining if any(
                    records[key]["classification"] not in matched_classes for key in templates[identifier]
                )}
            identifiers.extend(spdx.matching_templates(segment, remaining))
            keys = {key for identifier in identifiers for key in templates[identifier]}
            if not keys:
                normalized, _ = _canonicalize(segment)
                matches = index.match(query_string=normalized, approximate=False) if normalized else []
                eligible = {m.rule.license_expression for m in matches
                            if m.coverage() == 100 and m.score() == 100}
                exact = _complete_sequence(normalized, eligible, records)
                if exact is None:
                    continue
                keys.update(exact)
            previous_keys, previous_evidence = reachable[start]
            found_keys, found_evidence = reachable.get(end, (set(), []))
            item = {"start_line": start + 1, "end_line": end, "templates": identifiers,
                    "policies": sorted(keys)}
            reachable[end] = (found_keys | previous_keys | keys, found_evidence + previous_evidence + [item])
    complete = reachable.get(len(lines))
    return (sorted(complete[0]), complete[1]) if complete else (None, [])


def scan_candidates(files: dict[str, str], manifest: dict, index_path: Path) -> dict:
    """Return published/rejected/needs_review, metadata labels and auditable evidence.

    Files outside root candidates and root LICENSES/ are ignored. ScanCode is
    never asked to traverse a checkout, follow symlinks, or execute repository
    content. Callers must supply already-fetched regular UTF-8 Git blobs.
    """
    _validate_files(files)
    _runtime()
    index = _load_policy(manifest, index_path)
    records = manifest["policies"]
    candidates = {name: text for name, text in sorted(files.items()) if _is_candidate(name)}
    evidence = {"policy_index_sha256": manifest["index_sha256"], "files": [], "reason": ""}
    labels = set()
    classifications = set()
    uncertain = False
    for filename, text in candidates.items():
        normalized, ignored = _canonicalize(text)
        native_labels, native = _native_evidence(text)
        matches = index.match(query_string=normalized, approximate=False) if normalized else []
        eligible = {
            m.rule.license_expression for m in matches
            if m.coverage() == 100 and m.score() == 100 and m.rule.license_expression in records
        }
        complete = _complete_sequence(normalized, eligible, records)
        template_matches = []
        if complete is None:
            complete, template_matches = _template_sequence(text, native, native_labels, index, records)
        file_evidence = {
            "path": filename, "sha256": _sha(text), "ignored_notices": ignored,
            "native_matches": native, "native_labels": native_labels,
            "spdx_template_matches": template_matches,
            "policy_matches": [{
                "id": m.rule.license_expression, "rule": m.rule.identifier,
                "matcher": m.matcher, "coverage": m.coverage(), "score": m.score(),
            } for m in matches],
            "complete_text_coverage": complete is not None, "authorizing_policies": complete or [],
        }
        evidence["files"].append(file_evidence)
        if complete is None:
            uncertain = True
            labels.update(native_labels)
        else:
            for key in complete:
                classifications.add(records[key]["classification"])
                labels.update(records[key]["labels"])
    if not candidates:
        decision, reason = "rejected", "no_license_candidates"
    elif uncertain:
        decision, reason = "needs_review", "unknown_fragment_or_changed_clauses"
    elif classifications == {"whitelist"}:
        decision, reason = "published", "complete_whitelist_texts_only"
    elif classifications == {"blacklist"}:
        decision, reason = "rejected", "complete_blacklist_texts_only"
    else:
        decision, reason = "needs_review", "mixed_policy_classifications"
    evidence["reason"] = reason
    return {"decision": decision, "labels": sorted(labels), "evidence": evidence,
            "detector_version": manifest["detector_version"]}
