"""Git Database writes scoped to one configured public policy repository."""

import base64
import hashlib
import json
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx

from core.git import validate_git_url
from core.runtime import get_setting
from providers.clients import rate_limit_delay


class PolicyWriteError(Exception):
    def __init__(self, message, *, retryable=False, retry_after=0):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class BranchMoved(PolicyWriteError):
    pass


@dataclass
class PolicyHead:
    branch: str
    sha: str
    tree: str
    files: dict
    sources: dict
    digests: dict


class PolicyGitHub:
    def __init__(self, target, guard, *, transport=None):
        self.target = validate_git_url(target, github_only=True)
        self.repository = urlsplit(self.target).path.strip("/").removesuffix(".git")
        self.guard, self.transport = guard, transport

    def request(self, method, path="", **kwargs):
        # Re-read permissions, source and credentials at every external operation.
        self.guard()
        token = get_setting("license_write_token")
        if not token.startswith("github_pat_"):
            raise PolicyWriteError("请超管配置仅授权当前策略仓库、Contents 读写权限的细粒度令牌。")
        try:
            with httpx.Client(timeout=30, follow_redirects=False, transport=self.transport) as client:
                response = client.request(method, f"https://api.github.com/repos/{self.repository}{path}", headers={
                    "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                }, **kwargs)
        except httpx.HTTPError:
            raise PolicyWriteError("策略仓库请求超时或网络异常；重试前将回读远端结果。", retryable=True) from None
        if len(response.content) > 24 * 1024 * 1024:
            raise PolicyWriteError("策略仓库响应超过安全上限。")
        if method == "PATCH" and response.status_code in {409, 422}:
            raise BranchMoved("策略分支已前移，重新预检。")
        wait = rate_limit_delay(response)
        if wait is not None:
            raise PolicyWriteError("GitHub 限流，等待重试。", retryable=True, retry_after=wait)
        if response.status_code in {401, 403, 404}:
            raise PolicyWriteError("无法访问或写入当前策略仓库，请超管检查令牌权限与仓库配置。")
        if response.status_code == 429 or response.status_code >= 500:
            raise PolicyWriteError("GitHub 暂时不可用，等待重试。", retryable=True)
        if response.status_code not in {200, 201}:
            raise PolicyWriteError("GitHub 未接受策略仓库操作。")
        try:
            return response.json()
        except ValueError:
            raise PolicyWriteError("GitHub 响应格式无效。", retryable=True) from None

    def read(self):
        try:
            repo = self.request("GET")
            if repo.get("private") or repo["full_name"].lower() != self.repository.lower():
                raise PolicyWriteError("策略目标必须是当前配置的公开仓库。")
            branch = repo["default_branch"]
            head = self.request("GET", "/git/ref/heads/" + quote(branch, safe=""))["object"]["sha"]
            commit = self.request("GET", f"/git/commits/{head}")
            tree = self.request("GET", f"/git/trees/{commit['tree']['sha']}", params={"recursive": "1"})
            if tree.get("truncated"):
                raise PolicyWriteError("策略仓库文件列表不完整，不能预检。")
            files, sources, total, digests = {}, {}, 0, {}
            entries = [item for item in tree["tree"] if item["path"] == "sources.json" or (
                len(item["path"].split("/")) == 2 and item["path"].split("/")[0] in {"whitelist", "blacklist"}
            )]
            if len(entries) > 501:
                raise PolicyWriteError("策略仓库文件数量超过安全上限。")
            for item in entries:
                if item["type"] != "blob" or item["mode"] not in {"100644", "100755"}:
                    raise PolicyWriteError("策略文件必须是普通 Git 文件。")
                if item.get("size", 0) > 1_048_576:
                    raise PolicyWriteError("策略文件超过大小上限。")
                blob = self.request("GET", f"/git/blobs/{item['sha']}")
                if blob["encoding"] != "base64":
                    raise ValueError
                raw = base64.b64decode("".join(blob["content"].split()), validate=True)
                digests[item["path"]] = hashlib.sha256(raw).hexdigest()
                total += len(raw)
                if len(raw) > 1_048_576 or total > 16 * 1_048_576:
                    raise PolicyWriteError("策略正文超过大小上限。")
                content = raw.decode("utf-8-sig")
                if "\x00" in content:
                    raise ValueError
                if item["path"] == "sources.json":
                    sources = json.loads(content)
                    if (not isinstance(sources, dict) or not isinstance(sources.get("files"), list)
                            or not all(isinstance(entry, dict) for entry in sources["files"])):
                        raise ValueError
                else:
                    files[item["path"]] = content
            return PolicyHead(branch, head, commit["tree"]["sha"], files, sources, digests)
        except (KeyError, TypeError, ValueError):
            raise PolicyWriteError("策略仓库内容或来源清单格式无效。") from None

    def prepare_commit(self, head, original):
        path = f"whitelist/reviewed-{original.sha256}.txt"
        entries = [entry for entry in head.sources.get("files", []) if entry.get("path") != path]
        entries.append({
            "path": path, "sha256": original.sha256,
            "source": {"repository": original.source_url, "commit": original.source_sha, "path": original.path},
        })
        sources = {**head.sources, "files": entries}
        changes = []
        for filename, raw in ((path, bytes(original.raw)), ("sources.json", (json.dumps(sources, ensure_ascii=False, indent=2) + "\n").encode())):
            blob = self.request("POST", "/git/blobs", json={"content": base64.b64encode(raw).decode(), "encoding": "base64"})
            changes.append({"path": filename, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = self.request("POST", "/git/trees", json={"base_tree": head.tree, "tree": changes})
        commit = self.request("POST", "/git/commits", json={
            "message": f"Allow reviewed license text {original.sha256[:12]}",
            "tree": tree["sha"], "parents": [head.sha],
        })
        return commit["sha"]

    def advance(self, branch, sha):
        if self.request("GET").get("default_branch") != branch:
            raise BranchMoved("策略仓库默认分支已改变，重新预检。")
        self.request("PATCH", "/git/refs/heads/" + quote(branch, safe=""), json={"sha": sha, "force": False})

    @staticmethod
    def contains(head, original):
        path = f"whitelist/reviewed-{original.sha256}.txt"
        content = head.files.get(path)
        if content is None:
            return False
        # UTF-8 BOM is preserved in the submitted blob but not scanner inputs.
        if content != original.text or head.digests.get(path) != original.sha256:
            raise PolicyWriteError("确定性路径已存在不同正文，停止提交。")
        return any(entry.get("path") == path and entry.get("sha256") == original.sha256
                   and entry.get("source") for entry in head.sources.get("files", []))
