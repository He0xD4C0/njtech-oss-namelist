import hashlib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from django.conf import settings

HOSTS = {"github.com", "gitee.com", "gitlab.com"}
LICENSE_NAME = re.compile(r"^(licen[cs]e|copying|copyright|unlicense)(?:[._-].*)?$", re.I)


def validate_git_url(value, github_only=False):
    parsed = urlsplit(value.strip())
    allowed = {"github.com"} if github_only else HOSTS
    pieces = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(pieces) < 2
        or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in (".", "..") for part in pieces)
        or (parsed.hostname != "gitlab.com" and len(pieces) != 2)
    ):
        raise ValueError(
            "必须使用受支持平台的公开仓库 HTTPS URL / Use a supported public repository HTTPS URL"
        )
    path = "/".join(pieces).removesuffix(".git")
    return f"https://{parsed.hostname}/{path}.git"


@dataclass
class GitSnapshot:
    sha: str
    committed_at: datetime | None
    files: dict[str, str]
    decoding_errors: dict[str, str] = field(default_factory=dict)
    raw_files: dict[str, bytes] = field(default_factory=dict)


class GitLicenseInputError(ValueError):
    def __init__(self, sha, committed_at, reason, files):
        super().__init__("Repository license candidates exceed supported scan limits")
        self.snapshot = GitSnapshot(sha, committed_at, {})
        self.evidence = {"reason": reason, "files": files}


def fetch_snapshot(url, *, mode="licenses", branch=None, revision=None):
    url = validate_git_url(url, github_only=mode == "rules")
    settings.WORK_ROOT.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    with tempfile.TemporaryDirectory(prefix="git-", dir=settings.WORK_ROOT) as work:

        def git(*args, timeout=120):
            command = [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "protocol.file.allow=never",
                "-c",
                "protocol.ext.allow=never",
                "-c",
                "credential.helper=",
                "-C",
                work,
                *args,
            ]
            result = subprocess.run(command, env=env, check=True, capture_output=True, timeout=timeout)
            return result.stdout

        git("init", "--bare")
        # Public fetch only; no checkout, hooks, LFS, submodules or repository code.
        ref = "HEAD" if not branch else f"refs/heads/{branch}"
        if revision:
            if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision):
                raise ValueError("Invalid pinned revision")
            ref = revision
        if branch and (branch.startswith("-") or ".." in branch or any(c in branch for c in "~^:?*[\\")):
            raise ValueError("Invalid default branch")
        try:
            git("fetch", "--depth=1", "--filter=blob:none", "--no-tags", url, ref)
        except subprocess.CalledProcessError:
            if revision:
                raise
            # A successful public ref advertisement with no heads is an empty
            # repository, not a temporary network/authentication failure.
            if not git("ls-remote", "--heads", url).strip():
                return GitSnapshot("", None, {})
            raise
        sha = git("rev-parse", "FETCH_HEAD").decode().strip()
        if revision and sha != revision:
            raise ValueError("Pinned revision mismatch")
        committed_at = datetime.fromisoformat(git("show", "-s", "--format=%cI", sha).decode().strip())
        entries = git("ls-tree", "-z", sha).split(b"\0")
        selected = []
        for entry in filter(None, entries):
            info, raw_path = entry.split(b"\t", 1)
            mode_bits, kind, oid = info.decode().split()
            path = raw_path.decode("utf-8", errors="replace")
            if (
                kind == "blob"
                and mode_bits in ("100644", "100755")
                and mode == "licenses"
                and LICENSE_NAME.match(path)
            ):
                selected.append((path, oid))
            if kind == "tree" and path in (
                {"LICENSES"}
                if mode == "licenses"
                else ({"whitelist", "blacklist"} if mode == "rules" else {"zh-CN", "en-US"})
            ):
                # A single controlled directory depth; symlinks and Git submodules excluded.
                for child in filter(None, git("ls-tree", "-z", oid).split(b"\0")):
                    child_info, child_path = child.split(b"\t", 1)
                    child_mode, child_kind, child_oid = child_info.decode().split()
                    filename = child_path.decode("utf-8", errors="replace")
                    if child_kind == "blob" and child_mode in ("100644", "100755"):
                        if mode != "documents" or filename in {"privacy.md", "terms.md", "inclusion.md"}:
                            selected.append((f"{path}/{filename}", child_oid))
        if len(selected) > 500:
            if mode == "licenses":
                raise GitLicenseInputError(sha, committed_at, "too_many_license_candidates", [])
            raise ValueError("Too many policy/license files")
        files = {}
        raw_files = {}
        decoding_errors = {}
        total = 0
        for path, oid in selected:
            size = int(git("cat-file", "-s", oid))
            total += size
            if size > 1_048_576 or total > 16 * 1_048_576:
                if mode == "licenses":
                    raise GitLicenseInputError(
                        sha,
                        committed_at,
                        "license_text_exceeds_scan_limits",
                        [{"path": path, "git_blob": oid, "size": size}],
                    )
                raise ValueError("Policy/license text exceeds 1 MiB per file or 16 MiB total")
            raw = git("cat-file", "blob", oid)
            raw_files[path] = raw
            try:
                decoded = raw.decode("utf-8-sig")
                if "\x00" in decoded:
                    files[path] = ""
                    decoding_errors[path] = hashlib.sha256(raw).hexdigest()
                else:
                    files[path] = decoded
            except UnicodeDecodeError:
                # Retain a candidate and its real byte digest, never silently omit
                # unreadable terms or replace bytes with a falsely equivalent text.
                files[path] = ""
                decoding_errors[path] = hashlib.sha256(raw).hexdigest()
        return GitSnapshot(sha, committed_at, files, decoding_errors, raw_files)
