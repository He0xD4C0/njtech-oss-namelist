from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.markdown import render_markdown

from .models import Announcement, AnnouncementReceipt


def index(request):
    page = Paginator(Announcement.objects.all(), 20).get_page(request.GET.get("page"))
    return render(request, "bulletins/index.html", {"page_obj": page})


def detail(request, pk):
    announcement = get_object_or_404(Announcement, pk=pk)
    return render(
        request,
        "bulletins/detail.html",
        {"announcement": announcement, "rendered_body": render_markdown(announcement.body)},
    )


@login_required
@require_POST
def receipt(request, pk):
    try:
        revision = int(request.POST.get("revision", ""))
    except (ValueError, TypeError):
        return JsonResponse({"error": "invalid_revision"}, status=400)
    action = request.POST.get("action")
    with transaction.atomic():
        item = get_object_or_404(Announcement.objects.select_for_update(), pk=pk)
        if not item.targets(request.user):
            return JsonResponse({"error": "not_targeted"}, status=404)
        if item.expired:
            return JsonResponse({"error": "expired"}, status=410)
        if revision != item.revision:
            return JsonResponse({"error": "content_changed"}, status=409)
        if action not in ("confirm", "dismiss") or (
            item.kind == Announcement.Kind.POLICY and action != "confirm"
        ):
            return JsonResponse({"error": "confirmation_required"}, status=400)
        recorded, _created = AnnouncementReceipt.objects.get_or_create(announcement=item, user=request.user)
        recorded.delivered_at = recorded.delivered_at or timezone.now()
        if item.kind == Announcement.Kind.POLICY:
            recorded.confirmed_revision = item.revision
        recorded.save(update_fields=["delivered_at", "confirmed_revision"])
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "revision": item.revision})
    return redirect(reverse("bulletins:detail", args=[pk]))
