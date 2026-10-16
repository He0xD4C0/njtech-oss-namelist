from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import RuntimeSetting, User
from repositories.models import Repository, Submission

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("initialized_site")]


@pytest.fixture
def public_claim():
    user = User.objects.create_user(
        "alice", email="secret-alice@example.com", password="LocalTesting-password-42"
    )
    from allauth.account.models import EmailAddress
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    now = timezone.now()
    repository = Repository.objects.create(
        platform="github",
        platform_id="100",
        name="toolkit",
        full_name="alice/toolkit",
        owner_id="1",
        html_url="https://github.com/alice/toolkit",
        clone_url="https://github.com/alice/toolkit.git",
        description="A useful open-source toolkit",
        stars=13,
        commit_at=now,
        synced_at=now,
    )
    return Submission.objects.create(
        user=user,
        repository=repository,
        status="published",
        published_at=now,
        baseline_at=now,
        baseline_stars=13,
        license_labels=["MIT"],
        public_metadata={
            "name": "old-name",
            "full_name": "alice/last-public-name",
            "description": "Last public description",
            "html_url": "https://github.com/alice/last-public-name",
            "stars": 12,
            "fork": False,
            "archived": True,
        },
    )


def test_public_routes_and_privacy(client, public_claim):
    for path in ("/", "/repositories/", "/u/alice/", "/rankings/", "/announcements/"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert b"vendor/tabler/tabler.min.css" in response.content
        assert b"https://cdn" not in response.content
        assert public_claim.user.email.encode() not in response.content
    assert client.get("/repositories/100/").status_code == 404
    public_claim.user.public_email = True
    public_claim.user.save()
    assert b"mailto:secret-alice@example.com" in client.get("/u/alice/").content


def test_history_reads_preserved_public_metadata(client, public_claim):
    public_claim.status = "historical"
    public_claim.save()
    public_claim.repository.full_name = "private-owner/secret-title"
    public_claim.repository.description = "PRIVATE-DESCRIPTION"
    public_claim.repository.save()
    assert b"alice/last-public-name" not in client.get("/repositories/").content
    response = client.get("/repositories/", {"history": "1", "q": "last-public", "archived": "1"})
    assert b"alice/last-public-name" in response.content
    assert b"PRIVATE-DESCRIPTION" not in response.content
    assert b"private-owner" not in response.content


def test_never_approved_historical_claim_cannot_bypass_machine_review(client, public_claim):
    public_claim.status = "historical"
    public_claim.published_at = None
    public_claim.save()
    assert not client.get("/repositories/?history=1").context["page_obj"].paginator.count
    assert not client.get("/u/alice/?history=1").context["cards"]


def test_directory_filters_pagination_and_archived(client, public_claim):
    public_claim.repository.archived = True
    public_claim.repository.fork = True
    public_claim.repository.save()
    response = client.get(
        "/repositories/",
        {"q": "toolkit", "platform": "github", "license": "mit", "fork": "1", "archived": "1"},
    )
    assert response.context["page_obj"].paginator.count == 1
    for key, value in (("platform", "gitee"), ("fork", "0"), ("archived", "0"), ("license", "Apache")):
        assert client.get("/repositories/", {key: value}).context["page_obj"].paginator.count == 0
    repo = Repository.objects.create(
        platform="gitee",
        platform_id="101",
        name="second",
        full_name="alice/second",
        owner_id="1",
        html_url="https://gitee.com/alice/second",
        clone_url="https://gitee.com/alice/second.git",
    )
    Submission.objects.create(
        user=public_claim.user, repository=repo, status="published", published_at=timezone.now()
    )
    response = client.get("/repositories/", {"per_page": 1, "q": "alice"})
    assert response.context["page_obj"].paginator.num_pages == 2
    assert b"q=alice" in response.content and b"page=2" in response.content
    assert client.get("/repositories/", {"per_page": 1000}).context["page_obj"].paginator.per_page == 150
    assert client.get("/repositories/", {"per_page": "bad"}).context["page_obj"].paginator.per_page == 20
    assert client.get("/repositories/", {"page": "bad"}).status_code == 200


def test_home_limits_and_commit_order(client, public_claim):
    for number in range(12):
        repo = Repository.objects.create(
            platform="github",
            platform_id=str(number),
            name=str(number),
            full_name=f"alice/project-{number}",
            owner_id="1",
            html_url=f"https://github.com/alice/project-{number}",
            clone_url=f"https://github.com/alice/project-{number}.git",
            commit_at=timezone.now() - timedelta(days=number + 1),
        )
        Submission.objects.create(
            user=public_claim.user, repository=repo, status="published", published_at=timezone.now()
        )
    response = client.get("/")
    assert len(response.context["latest_cards"]) == len(response.context["active_cards"]) == 10
    assert response.context["active_cards"][0]["full_name"] == "alice/toolkit"


def test_removed_deleted_inactive_and_released_username(client, public_claim):
    public_claim.status = "removed"
    public_claim.save()
    assert b"alice/toolkit" not in client.get("/repositories/?history=1").content
    from django.core.exceptions import ValidationError
    public_claim.user.username = "alice-new"
    with pytest.raises(ValidationError):
        public_claim.user.save()
    public_claim.user.refresh_from_db()
    assert client.get("/u/alice/").status_code == 200
    assert client.get("/u/alice-new/").status_code == 404
    public_claim.user.delete()
    assert client.get("/u/alice/").status_code == 404


def test_ui_english_cookie_and_auth_readiness(client):
    client.cookies["django_language"] = "en"
    assert b"Explore repositories" in client.get("/").content
    assert b'<html lang="en">' in client.get("/").content
    response = client.get(reverse("account_signup"))
    assert response.status_code == 200
    assert b"Registration unavailable" in response.content
    assert b"SMTP" in response.content and b"Turnstile" in response.content
    RuntimeSetting.objects.create(key="turnstile_site_key", value="test-only-site-key")
    response = client.get(reverse("account_login"))
    assert b'data-sitekey="test-only-site-key"' in response.content
    assert b"cf-turnstile" in response.content
    response = client.post(reverse("account_login"), {"login": "nobody@example.com", "password": "bad"})
    assert response.status_code == 200
    assert b"errorlist" in response.content


def test_source_pending_config_and_license_download(client, settings):
    settings.SOURCE_REPOSITORY_URL = ""
    response = client.get("/source/")
    assert "源码仓库地址尚未配置" in response.content.decode()
    settings.SOURCE_REPOSITORY_URL = "https://github.com/example/namelist"
    assert b"https://github.com/example/namelist" in client.get("/source/").content
    license_response = client.get("/source/license/")
    assert license_response.status_code == 200
    assert b"GNU AFFERO GENERAL PUBLIC LICENSE" in b"".join(license_response.streaming_content)
