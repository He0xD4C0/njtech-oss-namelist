from datetime import date

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from bulletins.models import Announcement
from core.avatar import avatar_url
from repositories.models import PLATFORMS, Submission

from .cards import repository_card
from .rankings import SHANGHAI, available_months, month_start, results_for


def visible_claims(include_history=False):
    states = Q(status=Submission.Status.PUBLISHED)
    if include_history:
        states |= Q(status=Submission.Status.HISTORICAL, published_at__isnull=False)
    return Submission.objects.filter(states, user__is_active=True).select_related("user", "repository")


def paginated(request, objects):
    try:
        per_page = max(1, min(150, int(request.GET.get("per_page", 20))))
    except (ValueError, TypeError):
        per_page = 20
    page = Paginator(objects, per_page).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return page, query.urlencode()


def home(request):
    claims = visible_claims()
    return render(
        request,
        "directory/home.html",
        {
            "latest_cards": [repository_card(s) for s in claims.order_by("-published_at", "-pk")[:10]],
            "active_cards": [
                repository_card(s) for s in claims.order_by("-repository__commit_at", "-pk")[:10]
            ],
            "repository_count": claims.count(),
            "member_count": get_user_model().objects.filter(is_active=True).count(),
            "latest_announcement": Announcement.objects.filter(kind=Announcement.Kind.NORMAL)
            .order_by("-created_at")
            .first(),
        },
    )


def repositories(request):
    include_history = request.GET.get("history") == "1"
    claims = visible_claims(include_history).order_by("-published_at", "-pk")
    platform = request.GET.get("platform", "")
    if platform in dict(PLATFORMS):
        claims = claims.filter(repository__platform=platform)
    # Filter the last public facts, not the current transferred/private repository's metadata.
    keyword = request.GET.get("q", "").strip()[:200]
    fork = request.GET.get("fork", "")
    archived = request.GET.get("archived", "")
    license_query = request.GET.get("license", "").strip()[:150]
    cards = []
    for claim in claims:
        card = repository_card(claim)
        if (
            keyword
            and keyword.casefold()
            not in " ".join(str(card[key]) for key in ("full_name", "description")).casefold()
        ):
            continue
        if fork in ("0", "1") and bool(card["fork"]) != (fork == "1"):
            continue
        if archived in ("0", "1") and bool(card["archived"]) != (archived == "1"):
            continue
        if license_query and not any(
            license_query.casefold() in str(label).casefold() for label in card["license_labels"]
        ):
            continue
        cards.append(card)
    page, query = paginated(request, cards)
    return render(
        request,
        "directory/repositories.html",
        {
            "page_obj": page,
            "pagination_query": query,
            "platforms": PLATFORMS,
            "filters": {
                "q": keyword,
                "platform": platform,
                "license": license_query,
                "fork": fork,
                "archived": archived,
                "history": include_history,
            },
        },
    )


def profile(request, username):
    person = get_object_or_404(get_user_model(), username=username, is_active=True)
    claims = (
        visible_claims(request.GET.get("history") == "1").filter(user=person).order_by("-published_at", "-pk")
    )
    page, query = paginated(request, claims)
    cards = [repository_card(claim) for claim in page.object_list]
    return render(
        request,
        "directory/profile.html",
        {
            "person": person,
            "profile_avatar": avatar_url(person),
            "cards": cards,
            "page_obj": page,
            "pagination_query": query,
        },
    )


def rankings(request):
    months = available_months()
    raw = request.GET.get("month", "")
    try:
        month = date.fromisoformat(raw + "-01") if raw else month_start(timezone.localdate(timezone=SHANGHAI))
    except ValueError:
        raise Http404("Invalid month")
    if month not in months:
        raise Http404("No observations for this month")
    groups, frozen = results_for(month)
    return render(
        request,
        "directory/rankings.html",
        {
            "months": months,
            "selected_month": month,
            "frozen": frozen,
            "rank_groups": [{"platform": p, "label": label, "rows": groups[p]} for p, label in PLATFORMS],
        },
    )


def source(request):
    return render(
        request,
        "directory/source.html",
        {"repository_source_url": getattr(settings, "SOURCE_REPOSITORY_URL", "")},
    )


def license_text(request):
    return FileResponse(
        (settings.BASE_DIR / "LICENSE").open("rb"),
        as_attachment=True,
        filename="AGPL-3.0-only.txt",
        content_type="text/plain; charset=utf-8",
    )
