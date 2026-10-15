from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from repositories.models import PLATFORMS, StarSnapshot, Submission

from .cards import repository_card
from .models import RankingEntry, RankingMonth

SHANGHAI = ZoneInfo("Asia/Shanghai")


def month_start(value):
    return value.replace(day=1)


def next_month(value):
    return date(value.year + (value.month == 12), 1 if value.month == 12 else value.month + 1, 1)


def month_bounds(month):
    return (
        datetime.combine(month, datetime.min.time(), SHANGHAI),
        datetime.combine(next_month(month), datetime.min.time(), SHANGHAI),
    )


def live_results(month):
    start, end = month_bounds(month)
    groups = {key: [] for key, _label in PLATFORMS}
    claims = Submission.objects.filter(
        status=Submission.Status.PUBLISHED, baseline_at__lt=end, user__is_active=True
    ).select_related("repository", "user")
    for claim in claims:
        if claim.baseline_at is None or claim.baseline_stars is None:
            continue
        observations = list(
            claim.snapshots.filter(
                baseline_at=claim.baseline_at,
                observed_at__gte=max(start, claim.baseline_at),
                observed_at__lt=end,
            ).order_by("observed_at", "pk")
        )
        # A publication baseline is itself an actual observation, not a fabricated month start.
        if start <= claim.baseline_at < end:
            first_at, first_stars = claim.baseline_at, claim.baseline_stars
        elif observations:
            first_at, first_stars = observations[0].observed_at, observations[0].stars
        else:
            continue
        last_at, ending = (
            (observations[-1].observed_at, observations[-1].stars)
            if observations
            else (first_at, first_stars)
        )
        groups[claim.repository.platform].append(
            {
                "submission": claim,
                "gain": ending - first_stars,
                "ending_stars": ending,
                "first_observed_at": first_at,
                "last_observed_at": last_at,
                "card": repository_card(claim),
                "platform_id": claim.repository.platform_id,
            }
        )
    for platform, rows in groups.items():
        rows.sort(key=lambda item: (-item["gain"], -item["ending_stars"], item["platform_id"]))
        groups[platform] = rows[:10]
        for position, row in enumerate(groups[platform], 1):
            row["position"] = position
    return groups


def results_for(month):
    sealed = RankingMonth.objects.filter(month=month).first()
    if sealed is None:
        return live_results(month), False
    groups = {key: [] for key, _label in PLATFORMS}
    # Later loss of eligibility does not rewrite the seal. Explicit withdrawal does remove visibility.
    entries = (
        sealed.entries.filter(submission__isnull=False, submission__user__is_active=True)
        .exclude(submission__status=Submission.Status.REMOVED)
        .select_related("submission__repository", "submission__user")
    )
    for row in entries:
        groups[row.platform].append(
            {
                "position": row.position,
                "gain": row.gain,
                "ending_stars": row.ending_stars,
                "first_observed_at": row.first_observed_at,
                "last_observed_at": row.last_observed_at,
                "card": repository_card(row.submission, row.metadata),
            }
        )
    return groups, True


def available_months():
    months = set(RankingMonth.objects.values_list("month", flat=True))
    months.update(month_start(day) for day in StarSnapshot.objects.values_list("day", flat=True).distinct())
    months.update(
        month_start(timezone.localtime(at, SHANGHAI).date())
        for at in Submission.objects.exclude(baseline_at=None).values_list("baseline_at", flat=True)
    )
    months.add(month_start(timezone.localdate(timezone=SHANGHAI)))
    return sorted(months, reverse=True)


@transaction.atomic
def seal_month(month):
    if month >= month_start(timezone.localdate(timezone=SHANGHAI)):
        return False
    sealed, created = RankingMonth.objects.get_or_create(month=month)
    if not created:
        return False
    rows = []
    for platform, entries in live_results(month).items():
        for entry in entries:
            claim = entry["submission"]
            repo = claim.repository
            fields = (
                "name",
                "full_name",
                "description",
                "html_url",
                "fork",
                "archived",
                "upstream_url",
                "upstream_name",
                "commit_at",
            )
            # API refreshes may already have changed the Repository row. Seal the
            # retained public facts and this month's actual ending observation.
            facts = claim.public_metadata or {name: getattr(repo, name) for name in fields}
            metadata = {name: facts.get(name) for name in fields}
            if isinstance(metadata.get("commit_at"), datetime):
                metadata["commit_at"] = metadata["commit_at"].isoformat()
            metadata["stars"] = entry["ending_stars"]
            metadata["license_labels"] = claim.license_labels
            rows.append(
                RankingEntry(
                    month=sealed,
                    submission=claim,
                    platform=platform,
                    platform_id=repo.platform_id,
                    position=entry["position"],
                    gain=entry["gain"],
                    ending_stars=entry["ending_stars"],
                    first_observed_at=entry["first_observed_at"],
                    last_observed_at=entry["last_observed_at"],
                    metadata=metadata,
                )
            )
    RankingEntry.objects.bulk_create(rows)
    return True
