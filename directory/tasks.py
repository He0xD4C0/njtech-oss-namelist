from celery import shared_task
from django.utils import timezone

from repositories.models import StarSnapshot, Submission

from .models import RankingMonth
from .rankings import SHANGHAI, available_months, month_start, seal_month


@shared_task
def snapshot_stars():
    count = 0
    for claim in Submission.objects.filter(status="published", user__is_active=True).select_related(
        "repository"
    ):
        repo = claim.repository
        if claim.baseline_at is None or repo.synced_at is None or repo.synced_at < claim.baseline_at:
            continue
        # Do not timestamp stale provider values as though newly fetched today.
        day = timezone.localtime(repo.synced_at, SHANGHAI).date()
        _snapshot, created = StarSnapshot.objects.get_or_create(
            submission=claim,
            day=day,
            baseline_at=claim.baseline_at,
            defaults={"observed_at": repo.synced_at, "stars": repo.stars},
        )
        count += created
    return count


@shared_task
def close_months():
    current = month_start(timezone.localdate(timezone=SHANGHAI))
    sealed = set(RankingMonth.objects.values_list("month", flat=True))
    return sum(seal_month(month) for month in available_months() if month < current and month not in sealed)
