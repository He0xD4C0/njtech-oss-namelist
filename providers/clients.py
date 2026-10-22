"""Public-only platform adapters. OAuth is for identity, never for cloning private data."""

import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit

import httpx
from cryptography.fernet import InvalidToken
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.crypto import decrypt, encrypt
from core.i18n import text
from core.runtime import get_setting

from .models import OAuthBinding


class ProviderUnavailable(Exception):
    """Temporary/authentication/limit/invalid-response failures must not revoke eligibility."""

    def __init__(self, message, *, code="temporary", retryable=True, retry_after=0):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


def rate_limit_delay(response):
    """Primary headers and secondary-limit errors have different 403 meanings."""
    if response.status_code not in {403, 429}:
        return None
    try:
        message = str(response.json().get("message", "")).lower()
    except (ValueError, AttributeError):
        message = ""
    limited = (response.status_code == 429 or response.headers.get("X-RateLimit-Remaining") == "0"
               or "Retry-After" in response.headers or "rate limit" in message or "abuse detection" in message)
    if not limited:
        return None
    wait = 60
    try:
        wait = max(wait, int(response.headers.get("Retry-After", "0")))
        if response.headers.get("X-RateLimit-Remaining") == "0":
            wait = max(wait, int(response.headers.get("X-RateLimit-Reset", "0")) - int(timezone.now().timestamp()) + 1)
    except ValueError:
        pass
    return wait


def response_failure(response, *, refreshing=False):
    """Classify without retaining response bodies, URLs or credentials."""
    wait = rate_limit_delay(response)
    if wait is not None:
        return ProviderUnavailable("平台限流，稍后重试。", code="rate_limit", retry_after=wait)
    if response.status_code >= 500:
        return ProviderUnavailable("平台服务暂时不可用。")
    if response.status_code == 401 or (refreshing and response.status_code == 400):
        return ProviderUnavailable("授权已失效，等待账号重新授权。", code="reauthorization", retryable=False)
    if response.status_code == 403:
        return ProviderUnavailable("平台应用权限不足，请检查配置。", code="configuration", retryable=False)
    return ProviderUnavailable("平台响应异常，稍后重试。")


class RepositoryIneligible(Exception):
    """Definitively no longer a directly owned public Git repository."""


def parse_repository_url(value):
    """Accept a repository homepage/HTTPS clone URL, never an arbitrary fetch target."""
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        hosts = {"github.com": "github", "gitee.com": "gitee", "gitlab.com": "gitlab"}
        if parts.scheme != "https" or parts.netloc.lower() not in hosts:
            raise ValueError
        path = parts.path.removeprefix("/").removesuffix("/").removesuffix(".git")
        if (
            parts.query or parts.fragment
            or len(path.split("/")) != 2
            or not re.fullmatch(r"[A-Za-z0-9_.~-]+/[A-Za-z0-9_.~-]+", path)
            or any(part in {".", ".."} for part in path.split("/"))
        ):
            raise ValueError
    except ValueError:
        raise ValueError(text(
            "请输入 GitHub、Gitee 或 GitLab.com 的仓库主页地址，例如 https://github.com/用户名/仓库名。"
            "请使用 HTTPS 地址，不含文件路径、查询参数或账号密码。",
            "Enter a GitHub, Gitee or GitLab.com repository URL, such as https://github.com/username/repository. "
            "Use HTTPS without file paths, query parameters or credentials.",
        )) from None
    return hosts[parts.netloc.lower()], path


@dataclass(frozen=True)
class Identity:
    id: str
    username: str
    name: str = ""
    avatar_url: str = ""


@dataclass(frozen=True)
class RepoData:
    platform: str
    platform_id: str
    name: str
    full_name: str
    description: str
    html_url: str
    clone_url: str
    owner_id: str
    default_branch: str
    fork: bool
    archived: bool
    stars: int
    public: bool
    owner_type: str
    upstream_url: str = ""
    upstream_name: str = ""
    head_sha: str = ""
    commit_at: datetime | None = None

    def as_metadata(self):
        data = asdict(self)
        data.pop("public")
        data.pop("owner_type")
        return data


def stable_id(value):
    value = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise RepositoryIneligible("请选择平台返回的稳定仓库 ID。")
    return value


def response_id(value):
    try:
        return stable_id(value)
    except RepositoryIneligible:
        raise ProviderUnavailable("平台返回的稳定 ID 无效，未作失格判断。") from None


def canonical_url(url, host, *, clone=False):
    """Never accept a credential-bearing, redirected or cross-host clone/source URL."""
    try:
        parts = urlsplit(str(url))
        if (
            parts.scheme != "https"
            or parts.netloc != host
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError
        path = parts.path.removesuffix(".git").strip("/")
        if (
            len(path.split("/")) < 2
            or not re.fullmatch(r"[A-Za-z0-9_.~/-]+", path)
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise ValueError
        return f"https://{host}/{path}{'.git' if clone else ''}"
    except ValueError:
        raise ProviderUnavailable("平台返回了不可用的仓库地址；保留最近有效状态。") from None


class BaseClient:
    platform = ""
    api_url = ""
    host = ""
    token_url = ""

    def __init__(self, binding, *, transport=None):
        if binding.platform != self.platform:
            raise ValueError("Binding/platform mismatch")
        self.binding = binding
        self.transport = transport

    def _request(self, method, url, **kwargs):
        try:
            with httpx.Client(timeout=20, follow_redirects=False, transport=self.transport) as client:
                response = client.request(method, url, **kwargs)
        except httpx.HTTPError:
            raise ProviderUnavailable("平台暂时不可访问，请稍后重试。") from None
        if len(response.content) > 16 * 1024 * 1024:
            raise ProviderUnavailable("平台响应过大；保留最近有效状态。")
        return response

    def _refresh_locked(self, binding):
        refresh_token = decrypt(binding.encrypted_refresh_token) if binding.encrypted_refresh_token else ""
        if not self.token_url or not refresh_token:
            raise ProviderUnavailable("平台授权已过期，等待重新授权。", code="reauthorization", retryable=False)
        client_id = get_setting(f"oauth_{self.platform}_client_id")
        client_secret = get_setting(f"oauth_{self.platform}_client_secret")
        if not client_id or not client_secret:
            raise ProviderUnavailable("平台 OAuth 应用暂未配置。", code="configuration", retryable=False)
        response = self._request(
            "POST",
            self.token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Accept": "application/json"},
        )
        try:
            error_code = response.json().get("error")
        except (ValueError, AttributeError):
            error_code = None
        if error_code in {"incorrect_client_credentials", "invalid_client", "unauthorized_client"}:
            raise ProviderUnavailable("平台 OAuth 应用凭据无效。", code="configuration", retryable=False)
        if response.status_code != 200:
            raise response_failure(response, refreshing=True)
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            if data.get("error") in {"bad_refresh_token", "invalid_refresh_token", "invalid_grant", "expired_token"}:
                raise ProviderUnavailable("授权已失效，等待账号重新授权。", code="reauthorization", retryable=False)
            if data.get("error") in {"incorrect_client_credentials", "invalid_client", "unauthorized_client"}:
                raise ProviderUnavailable("平台 OAuth 应用凭据无效。", code="configuration", retryable=False)
            access = data["access_token"]
            if not isinstance(access, str) or not access:
                raise ValueError
            expires = data.get("expires_in")
            if expires is not None and int(expires) <= 0:
                raise ValueError
            expires_at = timezone.now() + timedelta(seconds=int(expires)) if expires else None
            refresh = data.get("refresh_token")
            if refresh is not None and (not isinstance(refresh, str) or not refresh):
                raise ValueError
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ProviderUnavailable("平台刷新响应无效；保留原凭据与最近有效状态。") from None
        binding.encrypted_access_token = encrypt(access)
        if refresh:
            binding.encrypted_refresh_token = encrypt(refresh)
        binding.expires_at = expires_at
        binding.save(
            update_fields=["encrypted_access_token", "encrypted_refresh_token", "expires_at", "updated_at"]
        )
        return access

    def _access_token(self, *, refresh=False, rejected_token=None):
        try:
            with transaction.atomic():
                binding = OAuthBinding.objects.select_for_update().get(pk=self.binding.pk)
                access = decrypt(binding.encrypted_access_token)
                if refresh and rejected_token is not None and access != rejected_token:
                    return access
                if refresh or (
                    binding.expires_at and binding.expires_at <= timezone.now() + timedelta(seconds=30)
                ):
                    return self._refresh_locked(binding)
                return access
        except OAuthBinding.DoesNotExist:
            raise ProviderUnavailable("平台已解绑。", code="reauthorization", retryable=False) from None
        except InvalidToken:
            raise ProviderUnavailable("平台凭据不可解密，需要服务器配置修复。", code="configuration", retryable=False) from None

    def _json(self, path, *, params=None, authenticated=False, repository=False, empty_commit=False):
        # Public GitHub reads still use this user's minimal OAuth identity budget,
        # not the 60/hour shared unauthenticated server-IP budget. This adds no
        # scope; private/organization repositories remain rejected before storage.
        authenticated = authenticated or self.platform == "github"
        headers = {"Accept": "application/json"}
        if authenticated:
            access = self._access_token()
            headers["Authorization"] = f"Bearer {access}"
        response = self._request("GET", self.api_url + path, params=params, headers=headers)
        if authenticated and response.status_code == 401 and self.token_url:
            headers["Authorization"] = f"Bearer {self._access_token(refresh=True, rejected_token=access)}"
            response = self._request("GET", self.api_url + path, params=params, headers=headers)
        if empty_commit and response.status_code in {404, 409}:
            return None
        if repository and response.status_code in {404, 410}:
            raise RepositoryIneligible("仓库已删除或不再公开。")
        if response.status_code != 200:
            # 401/403/429/5xx are never evidence that a repository became private.
            raise response_failure(response)
        try:
            return response.json()
        except ValueError:
            raise ProviderUnavailable("平台响应无效；保留最近有效状态。") from None

    def identity(self):
        data = self._json("/user", authenticated=True)
        try:
            identity = Identity(
                str(data["id"]),
                data["username" if self.platform == "gitlab" else "login"],
                data.get("name") or "",
                data.get("avatar_url") or "",
            )
            if identity.id != self.binding.platform_user_id or not identity.username:
                raise ValueError
            return identity
        except (KeyError, TypeError, ValueError):
            raise ProviderUnavailable("OAuth 身份与绑定不一致，请重新授权。", code="reauthorization", retryable=False) from None

    def _pages(self, path, params=None):
        for page in range(1, 1001):
            data = self._json(path, params={**(params or {}), "page": page, "per_page": 100})
            if not isinstance(data, list):
                raise ProviderUnavailable("平台仓库列表响应无效。")
            yield from data
            if len(data) < 100:
                return
        raise ProviderUnavailable("平台仓库列表超过安全分页上限，未作失格判断。")

    def _ensure_owned(self, repo, identity):
        if not repo.public or repo.owner_type != "user" or repo.owner_id != identity.id:
            raise RepositoryIneligible("仅可收录 OAuth 账号直接拥有的公开 Git 仓库。")
        return repo

    def _commit(self, repo, path):
        if not repo.default_branch:
            return repo
        data = self._json(path, empty_commit=True)
        if data is None:
            return repo
        try:
            if self.platform == "gitlab":
                sha, date = data["id"], data["committed_date"]
            else:
                sha, date = data["sha"], data["commit"]["committer"]["date"]
            at = parse_datetime(date)
            if (
                not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", sha)
                or not at
                or not timezone.is_aware(at)
            ):
                raise ValueError
            return replace(repo, head_sha=sha.lower(), commit_at=at)
        except (KeyError, TypeError, ValueError):
            raise ProviderUnavailable("默认分支提交响应无效；不使用仓库更新时间代替提交时间。") from None


class GitHubClient(BaseClient):
    token_url = "https://github.com/login/oauth/access_token"
    platform, host, api_url = "github", "github.com", "https://api.github.com"

    def _parse(self, data):
        try:
            owner = data["owner"]
            owner_id = response_id(owner["id"])
            owner_type = owner["type"].lower()
            if owner_type not in {"user", "organization"} or not isinstance(data["private"], bool):
                raise ValueError
            parent = data.get("parent") or {}
            html_url = canonical_url(data["html_url"], self.host)
            clone_url = canonical_url(data.get("clone_url") or html_url, self.host, clone=True)
            if clone_url != html_url + ".git" or data["full_name"] != urlsplit(html_url).path.lstrip("/"):
                raise ValueError
            return RepoData(
                platform=self.platform,
                platform_id=response_id(data["id"]),
                name=data["name"],
                full_name=data["full_name"],
                description=data.get("description") or "",
                html_url=html_url,
                clone_url=clone_url,
                owner_id=owner_id,
                default_branch=data.get("default_branch") or "",
                fork=bool(data.get("fork")),
                archived=bool(data.get("archived")),
                stars=int(data["stargazers_count"]),
                public=data.get("private") is False and data.get("visibility", "public") == "public",
                owner_type=owner_type,
                upstream_url=canonical_url(parent["html_url"], self.host)
                if parent.get("private") is False
                else "",
                upstream_name=parent.get("full_name", "") if parent.get("private") is False else "",
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ProviderUnavailable("平台仓库元数据无效。") from None

    def list_owned_public(self):
        identity = self.identity()
        repos = []
        for data in self._pages(
            f"/users/{quote(identity.username, safe='')}/repos", {"type": "owner", "sort": "full_name"}
        ):
            repo = self._parse(data)
            if repo.public and repo.owner_type == "user" and repo.owner_id == identity.id:
                repos.append(repo)
        return repos

    def repository(self, repository_id):
        identity = self.identity()
        rid = stable_id(repository_id)
        repo = self._ensure_owned(self._parse(self._json(f"/repositories/{rid}", repository=True)), identity)
        if repo.platform_id != rid:
            raise ProviderUnavailable("仓库稳定 ID 不一致。")
        return self._commit(
            repo, f"/repos/{quote(repo.full_name, safe='/')}/commits/{quote(repo.default_branch, safe='')}"
        )

    def repository_by_path(self, path):
        identity = self.identity()
        repo = self._ensure_owned(
            self._parse(self._json(f"/repos/{quote(path, safe='/')}", repository=True)), identity
        )
        return self._commit(
            repo, f"/repos/{quote(repo.full_name, safe='/')}/commits/{quote(repo.default_branch, safe='')}"
        )


class GiteeClient(GitHubClient):
    platform, host, api_url = "gitee", "gitee.com", "https://gitee.com/api/v5"
    token_url = "https://gitee.com/oauth/token"

    def _parse(self, data):
        repo = super()._parse(data)
        namespace = data.get("namespace") or {}
        if (
            not isinstance(namespace, dict)
            or namespace.get("type") not in {"personal", "enterprise", "group", "organization"}
            or not isinstance(data.get("public"), bool)
        ):
            raise ProviderUnavailable("Gitee 命名空间或公开状态响应无效，未作失格判断。")
        # Gitee enterprise/group owners may still report owner.type = User.
        return replace(
            repo,
            public=repo.public and data.get("public") is True and data.get("internal") is not True,
            owner_type="user" if namespace.get("type") == "personal" else "group",
        )

    def repository(self, repository_id):
        rid = stable_id(repository_id)
        # Gitee v5 has no documented immutable-ID repository lookup. Resolve the
        # ID from a fresh, complete public-owner list, then re-fetch its canonical path.
        selected = next((repo for repo in self.list_owned_public() if repo.platform_id == rid), None)
        if not selected:
            # Existing records provide a safe last-known path for definitive transfer/deletion checks.
            from repositories.models import Repository

            known = Repository.objects.filter(platform=self.platform, platform_id=rid).first()
            if known:
                path = urlsplit(canonical_url(known.html_url, self.host)).path
                data = self._json(f"/repos{path}", repository=True)
                repo = self._parse(data)
                if repo.platform_id != rid:
                    raise RepositoryIneligible("原地址已被其他仓库占用。")
                self._ensure_owned(repo, self.identity())
                # A successful single-object read can recover a private-profile list omission.
                selected = repo
            else:
                raise RepositoryIneligible("该稳定 ID 不在当前账号直接拥有的公开仓库列表中。")
        path = urlsplit(selected.html_url).path
        repo = self._ensure_owned(self._parse(self._json(f"/repos{path}", repository=True)), self.identity())
        if repo.platform_id != rid:
            raise RepositoryIneligible("原地址已被其他仓库占用。")
        return self._commit(repo, f"/repos{path}/commits/{quote(repo.default_branch, safe='')}")


class GitLabClient(BaseClient):
    platform, host, api_url = "gitlab", "gitlab.com", "https://gitlab.com/api/v4"
    token_url = "https://gitlab.com/oauth/token"

    def _parse(self, data, identity):
        try:
            namespace = data["namespace"]
            owner = data.get("owner") or {}
            if (
                namespace.get("kind") not in {"user", "group"}
                or not isinstance(namespace.get("path"), str)
                or not namespace["path"]
                or data.get("visibility") not in {"public", "private", "internal"}
            ):
                raise ValueError
            is_personal = namespace.get("kind") == "user"
            # namespace.id is NOT a user ID. Compare the fresh user's personal
            # namespace path, and owner.id when supplied by the public API.
            owns = is_personal and namespace.get("path") == identity.username
            owner_id = (
                response_id(owner["id"]) if owner.get("id") is not None else (identity.id if owns else "")
            )
            parent = data.get("forked_from_project") or {}
            html_url = canonical_url(data["web_url"], self.host)
            clone_url = canonical_url(data.get("http_url_to_repo") or html_url, self.host, clone=True)
            if clone_url != html_url + ".git" or data["path_with_namespace"] != urlsplit(
                html_url
            ).path.lstrip("/"):
                raise ValueError
            parent_public = parent.get("visibility") == "public"
            return RepoData(
                platform=self.platform,
                platform_id=response_id(data["id"]),
                name=data["name"],
                full_name=data["path_with_namespace"],
                description=data.get("description") or "",
                html_url=html_url,
                clone_url=clone_url,
                owner_id=owner_id if owns else "",
                default_branch=data.get("default_branch") or "",
                fork=bool(parent),
                archived=bool(data.get("archived")),
                stars=int(data["star_count"]),
                public=data.get("visibility") == "public"
                and data.get("repository_access_level", "enabled") not in {"private", "disabled"},
                owner_type="user" if is_personal else "group",
                upstream_url=canonical_url(parent["web_url"], self.host) if parent_public else "",
                upstream_name=parent.get("path_with_namespace", "") if parent_public else "",
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ProviderUnavailable("平台仓库元数据无效。") from None

    def list_owned_public(self):
        identity = self.identity()
        repos = []
        for data in self._pages(
            f"/users/{identity.id}/projects", {"visibility": "public", "order_by": "id", "sort": "asc"}
        ):
            repo = self._parse(data, identity)
            if repo.public and repo.owner_type == "user" and repo.owner_id == identity.id:
                repos.append(repo)
        return repos

    def repository(self, repository_id):
        identity = self.identity()
        rid = stable_id(repository_id)
        repo = self._ensure_owned(
            self._parse(self._json(f"/projects/{rid}", repository=True), identity), identity
        )
        if repo.platform_id != rid:
            raise ProviderUnavailable("仓库稳定 ID 不一致。")
        return self._commit(repo, f"/projects/{rid}/repository/commits/{quote(repo.default_branch, safe='')}")

    def repository_by_path(self, path):
        identity = self.identity()
        repo = self._ensure_owned(
            self._parse(self._json(f"/projects/{quote(path, safe='')}", repository=True), identity), identity
        )
        return self._commit(
            repo, f"/projects/{repo.platform_id}/repository/commits/{quote(repo.default_branch, safe='')}"
        )


CLIENTS = {"github": GitHubClient, "gitee": GiteeClient, "gitlab": GitLabClient}


def get_client(platform, binding, *, transport=None):
    try:
        client_class = CLIENTS[platform]
    except KeyError:
        raise RepositoryIneligible("不支持该平台。") from None
    return client_class(binding, transport=transport)


def validate_repository(binding, repository_id):
    return get_client(binding.platform, binding).repository(repository_id)
