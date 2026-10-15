from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

import pytest

from core.models import User
from directory.models import RankingEntry
from directory.rankings import available_months, live_results, month_bounds, results_for, seal_month
from directory.tasks import close_months, snapshot_stars
from repositories.models import Repository, StarSnapshot, Submission

pytestmark = pytest.mark.django_db
TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def claim_factory():
    user = User.objects.create_user("rank-user", email="rank@example.com")

    def make(stable_id="1", platform="github", baseline=None, stars=10, archived=False):
        baseline = baseline or datetime(2024, 1, 15, 12, tzinfo=TZ)
        repo = Repository.objects.create(
            platform=platform,
            platform_id=str(stable_id),
            name=f"repo-{stable_id}",
            full_name=f"rank/repo-{stable_id}",
            owner_id="owner",
            html_url=f"https://example.com/{platform}/{stable_id}",
            clone_url=f"https://example.com/{platform}/{stable_id}.git",
            archived=archived,
            stars=stars,
            synced_at=baseline,
        )
        return Submission.objects.create(
            user=user,
            repository=repo,
            status="published",
            baseline_at=baseline,
            baseline_stars=stars,
            published_at=baseline,
        )

    return make


def observe(claim, at, stars, baseline=None):
    return StarSnapshot.objects.create(
        submission=claim,
        day=at.astimezone(TZ).date(),
        observed_at=at,
        stars=stars,
        baseline_at=baseline or claim.baseline_at,
    )


def test_archived_zero_negative_and_independent_platform_top10(claim_factory):
    for index in range(12):
        claim = claim_factory(str(index), archived=index == 0)
        observe(claim, datetime(2024, 1, 31, 23, tzinfo=TZ), 20 - index)
    gitee = claim_factory("20", "gitee")
    gitlab = claim_factory("30", "gitlab")
    observe(gitee, datetime(2024, 1, 31, 23, tzinfo=TZ), 10)
    observe(gitlab, datetime(2024, 1, 31, 23, tzinfo=TZ), 4)
    groups = live_results(date(2024, 1, 1))
    assert len(groups["github"]) == 10
    assert groups["github"][0]["card"]["archived"] is True
    assert groups["gitee"][0]["gain"] == 0
    assert groups["gitlab"][0]["gain"] == -6


def test_midmonth_baseline_no_invented_history_and_shanghai_boundary(claim_factory):
    claim = claim_factory()
    observe(claim, datetime(2024, 1, 31, 15, 59, tzinfo=dt_timezone.utc), 20)
    observe(claim, datetime(2024, 1, 31, 16, 1, tzinfo=dt_timezone.utc), 30)
    observe(claim, datetime(2024, 2, 29, 23, tzinfo=TZ), 35)
    january = live_results(date(2024, 1, 1))["github"][0]
    february = live_results(date(2024, 2, 1))["github"][0]
    assert january["gain"] == 10 and january["first_observed_at"] == claim.baseline_at
    assert february["gain"] == 5
    start, end = month_bounds(date(2024, 1, 1))
    assert end.astimezone(dt_timezone.utc) == datetime(2024, 1, 31, 16, tzinfo=dt_timezone.utc)
    assert live_results(date(2023, 12, 1))["github"] == []


def test_tie_ending_stars_then_stable_id(claim_factory):
    one = claim_factory("2", stars=10)
    two = claim_factory("1", stars=10)
    three = claim_factory("3", stars=20)
    for claim, stars in ((one, 15), (two, 15), (three, 25)):
        observe(claim, datetime(2024, 1, 31, 23, tzinfo=TZ), stars)
    assert [row["platform_id"] for row in live_results(date(2024, 1, 1))["github"]] == ["3", "1", "2"]


def test_freeze_immutable_later_history_withdraw_and_delete(claim_factory):
    claim = claim_factory(archived=True)
    observe(claim, datetime(2024, 1, 31, 23, tzinfo=TZ), 18)
    assert seal_month(date(2024, 1, 1))
    assert not seal_month(date(2024, 1, 1))
    assert RankingEntry.objects.get().gain == 8
    claim.status = "historical"
    claim.save()
    claim.repository.stars = 1000
    claim.repository.save()
    groups, frozen = results_for(date(2024, 1, 1))
    assert frozen and groups["github"][0]["gain"] == 8
    assert groups["github"][0]["card"]["historical"]
    claim.status = "removed"
    claim.save()
    assert not results_for(date(2024, 1, 1))[0]["github"]
    assert RankingEntry.objects.count() == 1
    claim.user.delete()
    assert not results_for(date(2024, 1, 1))[0]["github"]


def test_republication_epoch_ignores_old_observations(claim_factory):
    claim = claim_factory()
    observe(claim, datetime(2024, 1, 20, tzinfo=TZ), 300)
    claim.baseline_at = datetime(2024, 1, 25, tzinfo=TZ)
    claim.baseline_stars = 100
    claim.save()
    observe(claim, datetime(2024, 1, 31, tzinfo=TZ), 102)
    assert live_results(date(2024, 1, 1))["github"][0]["gain"] == 2


def test_snapshot_idempotency_and_actual_observed_time(claim_factory):
    claim = claim_factory()
    claim.repository.synced_at = datetime(2024, 1, 16, 3, tzinfo=TZ)
    claim.repository.stars = 14
    claim.repository.save()
    assert snapshot_stars() == 1
    assert snapshot_stars() == 0
    snapshot = StarSnapshot.objects.get()
    assert snapshot.day == date(2024, 1, 16)
    assert snapshot.observed_at == claim.repository.synced_at
    assert snapshot.baseline_at == claim.baseline_at
    claim.repository.synced_at = claim.baseline_at - timedelta(seconds=1)
    claim.repository.save()
    assert snapshot_stars() == 0


def test_all_month_query_and_end_month_job(client, claim_factory):
    claim = claim_factory()
    observe(claim, datetime(2024, 2, 12, tzinfo=TZ), 14)
    assert date(2024, 1, 1) in available_months() and date(2024, 2, 1) in available_months()
    assert client.get("/rankings/?month=2024-01").status_code == 200
    assert client.get("/rankings/?month=invalid").status_code == 404
    assert client.get("/rankings/?month=2023-01").status_code == 404
    assert close_months() == 2
    assert close_months() == 0


def test_delayed_seal_uses_retained_facts_and_ending_observation(claim_factory):
    claim = claim_factory()
    claim.public_metadata = {
        "name": "old-name",
        "full_name": "rank/last-public",
        "description": "Last public facts",
        "html_url": "https://example.com/last-public",
        "archived": True,
        "fork": False,
        "commit_at": None,
    }
    claim.save()
    observe(claim, datetime(2024, 1, 31, 23, 35, tzinfo=TZ), 18)
    # Beat missed the boundary. A refreshed API value belongs to the next month.
    claim.repository.full_name = "new-owner/new-name"
    claim.repository.description = "New owner metadata"
    claim.repository.stars = 999
    claim.repository.save()
    observe(claim, datetime(2024, 2, 2, 12, tzinfo=TZ), 999)
    assert seal_month(date(2024, 1, 1))
    # Main calls sealing before the state mutation, even when the scheduler was offline.
    claim.status = "historical"
    claim.save()
    row = results_for(date(2024, 1, 1))[0]["github"][0]
    assert row["gain"] == 8 and row["ending_stars"] == 18
    assert row["card"]["full_name"] == "rank/last-public"
    assert row["card"]["description"] == "Last public facts"
    assert row["card"]["stars"] == 18 and row["card"]["historical"]
    assert not seal_month(date(2024, 1, 1))
