"""Local transport-mocked integration tests; these are NOT live OAuth/platform proof."""
import json
from datetime import timedelta
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import requests
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialLogin, SocialToken
from allauth.socialaccount.providers import registry
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from core.crypto import decrypt, encrypt
from core.models import PolicyConsent, PolicyDocument, SiteState
from core.runtime import set_setting
from providers.adapters import SCOPES, SocialAdapter
from providers.api import (
    GiteeClient,
    GitHubClient,
    GitLabClient,
    ProviderUnavailable,
    RepositoryIneligible,
    get_client,
)
from providers.clients import canonical_url
from providers.models import OAuthBinding
from providers.services import BindingConflict, save_binding

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated_rate_limits():
    cache.clear()


@pytest.fixture
def user():
    user = get_user_model().objects.create_user(username="alice", email="alice@example.test", password="RobustLocalPassword!4")
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    return user


def bind(user, platform="github", **kwargs):
    return OAuthBinding.objects.create(user=user, platform=platform, platform_user_id="7", username="alice",
                                      encrypted_access_token=encrypt("access-test-secret"), **kwargs)


def configure(platform):
    set_setting(f"oauth_{platform}_client_id", f"{platform}-app")
    set_setting(f"oauth_{platform}_client_secret", f"{platform}-secret")


def login_data(user, platform="github", uid="7", access="new-access-test", refresh="new-refresh-test"):
    data = {"id": int(uid), "login": "alice", "username": "alice", "name": "Alice", "avatar_url": ""}
    account = SocialAccount(user=user, provider=platform, uid=uid, extra_data=data)
    return SocialLogin(user=user, account=account, token=SocialToken(account=account, token=access, token_secret=refresh))


def gh_repo(**changes):
    data = {
        "id": 12, "name": "Example", "full_name": "alice/example", "description": "Public example",
        "html_url": "https://github.com/alice/example", "clone_url": "https://github.com/alice/example.git",
        "owner": {"id": 7, "login": "alice", "type": "User"}, "default_branch": "main",
        "fork": False, "archived": True, "private": False, "visibility": "public", "stargazers_count": 25,
    }
    data.update(changes)
    return data


def gitee_repo(**changes):
    data = gh_repo(html_url="https://gitee.com/alice/example.git", clone_url="https://gitee.com/alice/example.git",
                   namespace={"id": 888, "path": "alice", "type": "personal"}, public=True, internal=False)
    data.update(changes)
    return data


def gl_repo(**changes):
    data = {
        "id": 12, "name": "Example", "path_with_namespace": "alice/example", "description": "Public example",
        "web_url": "https://gitlab.com/alice/example", "http_url_to_repo": "https://gitlab.com/alice/example.git",
        "namespace": {"id": 9999, "kind": "user", "path": "alice"}, "default_branch": "main",
        "visibility": "public", "archived": True, "star_count": 25,
    }
    data.update(changes)
    return data


def commit_data(platform):
    if platform == "gitlab":
        return {"id": "a" * 40, "committed_date": "2026-02-01T00:10:00+08:00"}
    return {"sha": "a" * 40, "commit": {"committer": {"date": "2026-02-01T00:10:00+08:00"}}}


def transport_for(platform, repo, *, status=200, identity_id=7):
    def handle(request):
        if request.url.path.endswith("/user"):
            assert request.headers["authorization"] == "Bearer access-test-secret"
            assert "access_token" not in request.url.params
            return httpx.Response(200, json={"id": identity_id, "login": "alice", "username": "alice"})
        # Public-only validation is independent of authentication. GitHub uses
        # the minimal user OAuth budget; no provider token belongs in a URL.
        if request.url.host == "api.github.com":
            assert request.headers["authorization"] == "Bearer access-test-secret"
        else:
            assert "authorization" not in request.headers
        assert "access_token" not in request.url.params
        if request.url.path.endswith("/repos") or request.url.path.endswith("/projects"):
            return httpx.Response(status, json=[repo] if status == 200 else {"message": "error"})
        if "/commits/" in request.url.path:
            return httpx.Response(200, json=commit_data(platform))
        return httpx.Response(status, json=repo if status == 200 else {"message": "error"})
    return httpx.MockTransport(handle)


@pytest.mark.parametrize("platform,client_class,factory", [
    ("github", GitHubClient, gh_repo), ("gitee", GiteeClient, gitee_repo), ("gitlab", GitLabClient, gl_repo),
])
def test_public_direct_ownership_and_archived_is_eligible(user, platform, client_class, factory):
    binding = bind(user, platform)
    client = client_class(binding, transport=transport_for(platform, factory()))
    assert [repo.platform_id for repo in client.list_owned_public()] == ["12"]
    repo = client.repository("12")
    assert repo.archived is True
    assert repo.owner_id == "7"
    assert repo.public is True
    assert repo.head_sha == "a" * 40
    assert repo.commit_at.isoformat() == "2026-02-01T00:10:00+08:00"
    metadata = repo.as_metadata()
    assert isinstance(metadata["commit_at"], __import__("datetime").datetime)
    assert {"public", "owner_type"}.isdisjoint(metadata)
    assert repo.clone_url == repo.html_url + ".git"


@pytest.mark.parametrize("changes", [
    {"private": True}, {"owner": {"id": 8, "type": "User"}}, {"owner": {"id": 7, "type": "Organization"}},
])
def test_github_rejects_private_collaborator_or_organization(user, changes):
    binding = bind(user)
    client = GitHubClient(binding, transport=transport_for("github", gh_repo(**changes)))
    assert client.list_owned_public() == []
    with pytest.raises(RepositoryIneligible):
        client.repository("12")


def test_gitee_enterprise_owner_is_not_a_personal_repository(user):
    binding = bind(user, "gitee")
    data = gitee_repo(namespace={"type": "enterprise", "path": "alice"})
    client = GiteeClient(binding, transport=transport_for("gitee", data))
    assert client.list_owned_public() == []
    with pytest.raises(RepositoryIneligible):
        client.repository("12")


@pytest.mark.parametrize("changes", [
    {"visibility": "private"}, {"visibility": "internal"}, {"namespace": {"id": 7, "kind": "group", "path": "alice"}},
    {"namespace": {"id": 7, "kind": "user", "path": "bob"}}, {"owner": {"id": 8}},
    {"repository_access_level": "private"},
])
def test_gitlab_rejects_non_public_or_non_personal_owner(user, changes):
    binding = bind(user, "gitlab")
    client = GitLabClient(binding, transport=transport_for("gitlab", gl_repo(**changes)))
    assert client.list_owned_public() == []
    with pytest.raises(RepositoryIneligible):
        client.repository("12")


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_transient_api_failures_are_not_ineligibility(user, status):
    binding = bind(user)
    client = GitHubClient(binding, transport=transport_for("github", gh_repo(), status=status))
    with pytest.raises(ProviderUnavailable):
        client.repository("12")


@pytest.mark.parametrize("status", [404, 410])
def test_definitively_inaccessible_public_repository(user, status):
    binding = bind(user)
    client = GitHubClient(binding, transport=transport_for("github", gh_repo(), status=status))
    with pytest.raises(RepositoryIneligible):
        client.repository("12")


def test_wrong_token_identity_is_transient_not_transfer(user):
    binding = bind(user)
    client = GitHubClient(binding, transport=transport_for("github", gh_repo(), identity_id=99))
    with pytest.raises(ProviderUnavailable):
        client.repository("12")


@pytest.mark.parametrize("identifier", ["https://github.com/alice/example", "alice/example", "../user", "0", "12?token=x"])
def test_repository_input_is_stable_id_only(user, identifier):
    binding = bind(user)
    client = GitHubClient(binding, transport=transport_for("github", gh_repo()))
    with pytest.raises(RepositoryIneligible):
        client.repository(identifier)


@pytest.mark.parametrize("url", [
    "http://github.com/alice/example.git", "https://alice:secret@github.com/alice/example.git",
    "https://localhost/alice/example.git", "https://github.com.evil.test/alice/example.git",
    "https://github.com/alice/example?access_token=secret", "https://github.com/alice/../example.git",
])
def test_canonical_urls_never_include_credentials_or_cross_hosts(url):
    with pytest.raises(ProviderUnavailable):
        canonical_url(url, "github.com", clone=True)


def test_repository_metadata_url_is_revalidated(user):
    binding = bind(user)
    client = GitHubClient(binding, transport=transport_for("github", gh_repo(clone_url="https://evil.test/a/b.git")))
    with pytest.raises(ProviderUnavailable):
        client.repository("12")


def test_pagination_does_not_truncate_100th_repository(user):
    binding = bind(user)
    pages = []
    def handle(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 7, "login": "alice"})
        page = request.url.params["page"]
        pages.append(page)
        if page == "1":
            return httpx.Response(200, json=[gh_repo(id=i) for i in range(1, 101)])
        return httpx.Response(200, json=[gh_repo(id=101)])
    client = GitHubClient(binding, transport=httpx.MockTransport(handle))
    assert len(client.list_owned_public()) == 101
    assert pages == ["1", "2"]


def test_same_user_platform_and_external_identity_constraints(user):
    bind(user)
    with pytest.raises(IntegrityError), transaction.atomic():
        OAuthBinding.objects.create(user=user, platform="github", platform_user_id="8", username="other", encrypted_access_token="x")
    other = get_user_model().objects.create_user(username="other", email="other@example.test")
    with pytest.raises(IntegrityError), transaction.atomic():
        OAuthBinding.objects.create(user=other, platform="github", platform_user_id="7", username="alice", encrypted_access_token="x")


def test_save_binding_encrypts_and_retains_refresh_token(user):
    login = login_data(user)
    binding = save_binding(login, user)
    assert binding.encrypted_access_token != login.token.token
    assert decrypt(binding.encrypted_access_token) == login.token.token
    assert decrypt(binding.encrypted_refresh_token) == login.token.token_secret
    login.token.token = "updated-access-test"
    login.token.token_secret = ""
    binding = save_binding(login, user)
    assert decrypt(binding.encrypted_access_token) == "updated-access-test"
    assert decrypt(binding.encrypted_refresh_token) == "new-refresh-test"
    assert not SocialToken.objects.exists()


def test_save_binding_refuses_second_platform_identity(user):
    save_binding(login_data(user), user)
    with pytest.raises(BindingConflict):
        save_binding(login_data(user, uid="8"), user)
    assert OAuthBinding.objects.count() == 1


def test_pending_signup_session_uses_encrypted_tokens(user):
    configure("github")
    adapter = SocialAdapter()
    token = SocialToken(token="pending-access-secret", token_secret="pending-refresh-secret")
    serialized = adapter.serialize_instance(token)
    assert "pending-access-secret" not in json.dumps(serialized)
    assert "pending-refresh-secret" not in json.dumps(serialized)
    restored = adapter.deserialize_instance(SocialToken, serialized)
    assert restored.token == "pending-access-secret"
    assert restored.token_secret == "pending-refresh-secret"
    with pytest.raises(ValueError):
        adapter.deserialize_instance(SocialToken, {"token": "unsafe-plaintext"})


def test_runtime_social_app_is_not_saved_and_ignores_plaintext_socialapp(user):
    configure("github")
    SocialApp.objects.create(provider="github", name="Ignored", client_id="plain-app", secret="plain-secret")
    apps = SocialAdapter().list_apps(None, provider="github")
    assert len(apps) == 1
    assert apps[0].pk is None
    assert apps[0].client_id == "github-app"
    assert apps[0].secret == "github-secret"
    assert apps[0].settings["scope"] == ["read:user"]
    assert SocialAdapter().list_apps(None, provider="github", client_id="wrong-app") == []


@pytest.mark.parametrize("platform", ["github", "gitee", "gitlab"])
def test_minimal_scope_cannot_be_extended_from_query(platform):
    configure(platform)
    request = RequestFactory().get("/", {"scope": "repo,api,projects", "auth_params": "scope=api&redirect_uri=https://evil.test"})
    provider = registry.get_class(platform)(request, app=SocialAdapter().list_apps(request, provider=platform)[0])
    assert provider.get_scope_from_request(request) == SCOPES[platform]
    assert provider.get_auth_params_from_request(request, None) == {}


def test_provider_verified_email_is_not_site_verification(user):
    login = login_data(user)
    login.email_addresses = [EmailAddress(email="provided@example.test", verified=True, primary=True)]
    request = RequestFactory().get("/")
    request.user = user
    SocialAdapter().pre_social_login(request, login)
    assert login.email_addresses[0].verified is False
    assert SocialAdapter().is_email_verified(None, "provided@example.test") is False
    assert SocialAdapter().can_authenticate_by_email(login, "provided@example.test") is False


@pytest.mark.parametrize("platform", ["gitee", "gitlab"])
def test_expired_token_refresh_is_encrypted_and_post_only(user, platform):
    configure(platform)
    binding = bind(user, platform, encrypted_refresh_token=encrypt("old-refresh-secret"), expires_at=timezone.now() - timedelta(minutes=1))
    requests_seen = []
    def handle(request):
        requests_seen.append(request)
        assert "secret" not in str(request.url)
        if request.method == "POST":
            assert request.url.path == "/oauth/token"
            assert b"old-refresh-secret" in request.content
            return httpx.Response(200, json={"access_token": "refreshed-access", "refresh_token": "rotated-refresh", "expires_in": 7200})
        assert request.headers["authorization"] == "Bearer refreshed-access"
        return httpx.Response(200, json={"id": 7, "login": "alice", "username": "alice"})
    assert get_client(platform, binding, transport=httpx.MockTransport(handle)).identity().id == "7"
    binding.refresh_from_db()
    assert decrypt(binding.encrypted_access_token) == "refreshed-access"
    assert decrypt(binding.encrypted_refresh_token) == "rotated-refresh"
    assert binding.expires_at > timezone.now()
    assert len(requests_seen) == 2


def test_refresh_failure_keeps_last_credentials(user):
    configure("gitlab")
    binding = bind(user, "gitlab", encrypted_refresh_token=encrypt("old-refresh-secret"), expires_at=timezone.now() - timedelta(minutes=1))
    original = binding.encrypted_access_token
    client = GitLabClient(binding, transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    with pytest.raises(ProviderUnavailable):
        client.identity()
    binding.refresh_from_db()
    assert binding.encrypted_access_token == original


def test_unlink_prevents_use_of_stale_binding_object(user):
    binding = bind(user)
    binding_id = binding.pk
    OAuthBinding.objects.filter(pk=binding_id).delete()
    client = GitHubClient(binding, transport=httpx.MockTransport(lambda request: pytest.fail("No network after unlink")))
    with pytest.raises(ProviderUnavailable):
        client.identity()


def test_disconnect_requires_post_confirmation_and_csrf(user):
    bind(user)
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    assert client.get(reverse("providers:disconnect")).status_code == 405
    assert client.post(reverse("providers:disconnect"), {"platform": "github", "confirm": "on"}).status_code == 403
    assert OAuthBinding.objects.count() == 1


def test_disconnect_withdraws_all_claims_immediately(user, client):
    from repositories.models import Repository, Submission
    bind(user)
    SocialAccount.objects.create(user=user, provider="github", uid="7")
    repo = Repository.objects.create(platform="github", platform_id="12", name="Example", full_name="alice/example",
                                     html_url="https://github.com/alice/example", clone_url="https://github.com/alice/example.git", owner_id="7")
    claim = Submission.objects.create(user=user, repository=repo, status="published", generation=3)
    client.force_login(user)
    assert client.post(reverse("providers:disconnect"), {"platform": "github"}).status_code == 400
    assert client.post(reverse("providers:disconnect"), {"platform": "github", "confirm": "on"}).status_code == 302
    claim.refresh_from_db()
    assert claim.status == "removed" and claim.generation == 4
    assert not OAuthBinding.objects.exists()
    assert not SocialAccount.objects.exists()


def mock_allauth_exchange(monkeypatch, platform, uid=7):
    seen = []
    def response_for(session, method, url, **kwargs):
        seen.append((method, url, kwargs))
        assert "access_token" not in parse_qs(urlsplit(url).query)
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers["Content-Type"] = "application/json"
        if "/token" in url or "/access_token" in url:
            data = {"access_token": "callback-access-secret", "refresh_token": "callback-refresh-secret", "token_type": "bearer"}
        else:
            assert kwargs["headers"]["Authorization"] == "Bearer callback-access-secret"
            data = {"id": uid, "login": "alice", "username": "alice", "email": "platform@example.test", "name": "Alice", "avatar_url": ""}
        response._content = json.dumps(data).encode()
        return response
    monkeypatch.setattr(requests.Session, "request", response_for)
    return seen


@pytest.mark.parametrize("platform", ["github", "gitee", "gitlab"])
def test_allauth_oauth_connect_actual_routes_state_and_encrypted_persistence(user, client, monkeypatch, platform):
    configure(platform)
    seen = mock_allauth_exchange(monkeypatch, platform)
    client.force_login(user)
    begin = client.post(reverse(f"{platform}_login") + "?process=connect")
    assert begin.status_code == 302
    params = parse_qs(urlsplit(begin["Location"]).query)
    assert params["scope"][0].split() == SCOPES[platform]
    complete = client.get(reverse(f"{platform}_callback"), {"code": "local-test-code", "state": params["state"][0]})
    assert complete.status_code == 302
    binding = OAuthBinding.objects.get(user=user, platform=platform)
    assert binding.platform_user_id == "7"
    assert decrypt(binding.encrypted_access_token) == "callback-access-secret"
    assert decrypt(binding.encrypted_refresh_token) == "callback-refresh-secret"
    assert not SocialToken.objects.exists()
    assert SocialAccount.objects.get(user=user, provider=platform).uid == "7"
    assert len(seen) == 2  # token and profile only, no /user/emails request


def test_callback_without_valid_state_does_not_bind_or_exchange(user, client, monkeypatch):
    configure("github")
    seen = mock_allauth_exchange(monkeypatch, "github")
    client.force_login(user)
    response = client.get(reverse("github_callback"), {"code": "local-test-code", "state": "invalid-state"})
    assert response.status_code == 401
    assert not OAuthBinding.objects.exists() and seen == []


def test_oauth_initiation_does_not_skip_csrf(user):
    configure("github")
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    assert client.post(reverse("github_login") + "?process=connect").status_code == 403


def test_oauth_anonymous_login_requires_turnstile(client):
    configure("github")
    response = client.post(reverse("github_login"))
    assert response.status_code == 400
    assert "安全验证" in response.content.decode()
    assert "socialaccount_states" not in client.session


@pytest.mark.parametrize("platform", ["github", "gitee", "gitlab"])
def test_unconfigured_platform_routes_are_disabled(client, platform):
    assert client.get(reverse(f"{platform}_login")).status_code == 404
    assert client.get(reverse(f"{platform}_callback")).status_code == 404


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_full_social_signup_password_local_email_consent_and_encrypted_pending_session(client, monkeypatch):
    configure("github")
    SiteState.objects.create(pk=1, activated_at=timezone.now())
    set_setting("smtp_host", "smtp.example.test")
    set_setting("smtp_verified", "1")
    set_setting("turnstile_site_key", "local-test-key")
    set_setting("turnstile_secret_key", "local-test-secret")
    for kind in ("privacy", "terms", "inclusion"):
        PolicyDocument.objects.create(kind=kind, language="zh-CN", body="Local test policy", source_sha="b" * 40)
    monkeypatch.setattr("providers.oauth_views.verify_turnstile", lambda request: None)
    monkeypatch.setattr("core.forms.verify_turnstile", lambda request: None)
    imported = Mock()
    monkeypatch.setattr("core.services.import_oauth_profile", imported)
    mock_allauth_exchange(monkeypatch, "github")
    begin = client.post(reverse("github_login"))
    state = parse_qs(urlsplit(begin["Location"]).query)["state"][0]
    callback = client.get(reverse("github_callback"), {"code": "local-test-code", "state": state})
    assert callback.status_code == 302 and callback["Location"] == reverse("socialaccount_signup")
    pending = client.session["socialaccount_sociallogin"]
    assert "callback-access-secret" not in json.dumps(pending)
    assert "callback-refresh-secret" not in json.dumps(pending)
    assert "github-secret" not in json.dumps(pending)
    response = client.post(reverse("socialaccount_signup"), {
        "username": "newuser", "email": "newuser@example.test", "password1": "RobustLocalPassword!4",
        "password2": "RobustLocalPassword!4", "policy_language": "zh-CN", "accept_policies": "on",
        "policy_versions": json.dumps({"zh-CN": {kind: "b" * 40 for kind in ("privacy", "terms", "inclusion")}}),
    })
    assert response.status_code == 302
    user = get_user_model().objects.get(username="newuser")
    assert user.check_password("RobustLocalPassword!4")
    assert not EmailAddress.objects.filter(user=user, verified=True).exists()
    assert PolicyConsent.objects.filter(user=user).count() == 1
    assert OAuthBinding.objects.get(user=user).platform_user_id == "7"
    imported.assert_called_once()
    assert not SocialToken.objects.exists()


def test_binding_page_visible_with_disabled_platforms(user, client):
    client.force_login(user)
    response = client.get(reverse("providers:index"))
    assert response.status_code == 200
    body = response.content.decode()
    assert all(platform in body for platform in ["GitHub", "Gitee", "GitLab.com"])
    assert body.count("disabled") >= 3


def test_no_raw_allauth_credentials_or_identity_admin_bypass(settings):
    from django.contrib import admin
    assert settings.SOCIALACCOUNT_STORE_TOKENS is False
    assert not admin.site.is_registered(SocialApp)
    assert not admin.site.is_registered(SocialToken)
    assert not admin.site.is_registered(SocialAccount)


def connect_callback(client, platform):
    begin = client.post(reverse(f"{platform}_login") + "?process=connect")
    assert begin.status_code == 302
    state = parse_qs(urlsplit(begin["Location"]).query)["state"][0]
    return client.get(reverse(f"{platform}_callback"), {"code": "local-test-code", "state": state})


def test_reauthorization_updates_token_but_not_registration_profile(user, client, monkeypatch):
    configure("github")
    bind(user)
    SocialAccount.objects.create(user=user, provider="github", uid="7")
    imported = Mock()
    monkeypatch.setattr("core.services.import_oauth_profile", imported)
    mock_allauth_exchange(monkeypatch, "github")
    client.force_login(user)
    assert connect_callback(client, "github").status_code == 302
    assert OAuthBinding.objects.count() == 1
    assert SocialAccount.objects.count() == 1
    assert decrypt(OAuthBinding.objects.get(user=user).encrypted_access_token) == "callback-access-secret"
    imported.assert_not_called()


def test_connect_second_identity_same_platform_does_not_replace_first(user, client, monkeypatch):
    configure("github")
    binding = bind(user)
    SocialAccount.objects.create(user=user, provider="github", uid="7")
    mock_allauth_exchange(monkeypatch, "github", uid=8)
    client.force_login(user)
    assert connect_callback(client, "github").status_code == 302
    binding.refresh_from_db()
    assert binding.platform_user_id == "7"
    assert decrypt(binding.encrypted_access_token) == "access-test-secret"
    assert SocialAccount.objects.count() == 1


def test_cross_user_connect_never_reassigns_identity(user, client, monkeypatch):
    configure("github")
    bind(user)
    SocialAccount.objects.create(user=user, provider="github", uid="7")
    other = get_user_model().objects.create_user(username="bob", email="bob@example.test", password="RobustLocalPassword!4")
    EmailAddress.objects.create(user=other, email=other.email, primary=True, verified=True)
    mock_allauth_exchange(monkeypatch, "github")
    client.force_login(other)
    assert connect_callback(client, "github").status_code == 302
    assert not OAuthBinding.objects.filter(user=other).exists()
    assert OAuthBinding.objects.get(platform="github", platform_user_id="7").user_id == user.pk
    assert SocialAccount.objects.get(provider="github", uid="7").user_id == user.pk


def test_allauth_default_disconnect_also_withdraws_claims(user, client):
    from repositories.models import Repository, Submission
    bind(user)
    account = SocialAccount.objects.create(user=user, provider="github", uid="7")
    repo = Repository.objects.create(platform="github", platform_id="12", name="Example", full_name="alice/example",
                                     html_url="https://github.com/alice/example", clone_url="https://github.com/alice/example.git", owner_id="7")
    claim = Submission.objects.create(user=user, repository=repo, status="published")
    client.force_login(user)
    response = client.post(reverse("socialaccount_connections"), {"account": account.pk})
    assert response.status_code == 302
    claim.refresh_from_db()
    assert claim.status == "removed"
    assert not OAuthBinding.objects.exists()


@pytest.mark.parametrize("changes", [{"id": None}, {"owner": {"id": None, "type": "User"}}, {"private": None}])
def test_invalid_metadata_cannot_become_false_ineligibility(user, changes):
    client = GitHubClient(bind(user), transport=transport_for("github", gh_repo(**changes)))
    with pytest.raises(ProviderUnavailable):
        client.repository("12")


def test_upstream_private_information_is_not_exposed(user):
    parent = {"private": True, "html_url": "https://github.com/private-owner/private-source", "full_name": "private-owner/private-source"}
    client = GitHubClient(bind(user), transport=transport_for("github", gh_repo(fork=True, parent=parent)))
    repo = client.repository("12")
    assert repo.fork is True
    assert repo.upstream_name == repo.upstream_url == ""


def test_commit_time_is_committer_date_not_repository_updated_at(user):
    data = gh_repo(updated_at="2026-03-15T01:02:00+08:00")
    client = GitHubClient(bind(user), transport=transport_for("github", data))
    assert client.repository("12").commit_at.isoformat() == "2026-02-01T00:10:00+08:00"


def test_malformed_oauth_profile_is_graceful_and_leaves_no_binding(user, client, monkeypatch):
    configure("github")
    def malformed(session, method, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        data = {"access_token": "callback-access-secret"} if "access_token" in url else {"id": None}
        response._content = json.dumps(data).encode()
        return response
    monkeypatch.setattr(requests.Session, "request", malformed)
    client.force_login(user)
    assert connect_callback(client, "github").status_code == 401
    assert not OAuthBinding.objects.exists()


def test_oauth_post_rate_limit_before_external_authorization(user, client):
    configure("github")
    client.force_login(user)
    for _ in range(10):
        assert client.post(reverse("github_login") + "?process=connect").status_code == 302
    assert client.post(reverse("github_login") + "?process=connect").status_code == 403


def test_compat_connections_redirects_and_prevents_other_users_disconnection(user, client):
    other = get_user_model().objects.create_user(username="bob", email="bob@example.test")
    account = SocialAccount.objects.create(user=other, provider="github", uid="8")
    bind(other)
    client.force_login(user)
    assert client.get(reverse("socialaccount_connections")).status_code == 302
    assert client.post(reverse("socialaccount_connections"), {"account": account.pk}).status_code == 400
    assert SocialAccount.objects.filter(pk=account.pk).exists()
    assert OAuthBinding.objects.filter(user=other).exists()


@pytest.mark.e2e
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("width,height", [(1440, 1000), (390, 844)])
def test_browser_platform_cards_and_disconnect(user, client, live_server, tmp_path, width, height):
    """Real browser, real local HTTP/CSRF; external providers remain unconfigured."""
    from django.conf import settings
    from playwright.sync_api import expect, sync_playwright
    binding = bind(user)
    SocialAccount.objects.create(user=user, provider="github", uid="7")
    client.force_login(user)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": width, "height": height})
        context.add_cookies([{"name": settings.SESSION_COOKIE_NAME,
                             "value": client.cookies[settings.SESSION_COOKIE_NAME].value, "url": live_server.url}])
        page = context.new_page()
        page.goto(live_server.url + reverse("providers:index"))
        expect(page.get_by_role("heading", name="仓库平台绑定", exact=True)).to_be_visible()
        expect(page.get_by_role("heading", name="GitHub", exact=True)).to_be_visible()
        expect(page.get_by_role("heading", name="Gitee", exact=True)).to_be_visible()
        expect(page.get_by_role("heading", name="GitLab.com", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="选择仓库", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.get_by_text("解绑此平台", exact=True).click()
        confirmation = page.get_by_role("checkbox", name="我确认立即撤下该平台全部公开收录内容", exact=True)
        expect(confirmation).to_be_visible()
        screenshot = tmp_path / f"platforms-{width}.png"
        page.screenshot(path=str(screenshot), full_page=True)
        print(f"Provider browser evidence: {screenshot}")
        confirmation.check()
        page.get_by_role("button", name="确认解绑", exact=True).click()
        expect(page.get_by_text("平台已解绑，相关公开内容已撤下。", exact=False)).to_be_visible()
        expect(page.get_by_role("link", name="选择仓库", exact=True)).to_have_count(0)
        browser.close()
    assert not OAuthBinding.objects.filter(pk=binding.pk).exists()
