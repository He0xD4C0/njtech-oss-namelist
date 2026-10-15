from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from .models import Submission


@login_required
@ratelimit(key="user", rate="20/m", block=True)
def index(request):
    from providers.api import ProviderUnavailable, RepositoryIneligible, get_client
    from providers.models import OAuthBinding

    platform = request.GET.get("platform", "")
    candidates = []
    bindings = OAuthBinding.objects.filter(user=request.user)
    binding = bindings.filter(platform=platform).first()
    if binding:
        try:
            candidates = get_client(platform, binding).list_owned_public()
        except (ProviderUnavailable, RepositoryIneligible):
            messages.error(request, "平台暂不可用，请稍后重试 / Provider unavailable, retry later")
    return render(
        request,
        "repositories/manage.html",
        {
            "submissions": request.user.submissions.exclude(status="removed")
            .select_related("repository")
            .order_by("-created_at"),
            "bindings": bindings,
            "candidates": candidates,
            "platform": platform,
        },
    )


@login_required
@require_POST
@ratelimit(key="user", rate="10/m", block=True)
def add(request):
    from providers.api import ProviderUnavailable, RepositoryIneligible
    from providers.models import OAuthBinding

    from .services import add_submission

    try:
        add_submission(request.user, request.POST.get("platform", ""), request.POST.get("repository_id", ""))
        messages.success(request, "仓库已进入机审队列 / Repository queued for automated review")
    except (ValueError, OAuthBinding.DoesNotExist, RepositoryIneligible):
        messages.error(
            request,
            "无法添加：请检查配额、绑定及直接拥有的公开仓库 / Cannot add: check quota, binding and directly owned public repository",
        )
    except ProviderUnavailable:
        messages.error(
            request, "平台暂不可用，未添加仓库 / Provider temporarily unavailable; repository not added"
        )
    return redirect("repositories:index")


@login_required
@require_POST
def remove(request, pk):
    submission = get_object_or_404(Submission, pk=pk, user=request.user)
    from .services import remove_submission

    remove_submission(submission, request.user)
    return redirect("repositories:index")


@login_required
@require_POST
@ratelimit(key="user", rate="10/m", block=True)
def recheck(request, pk):
    submission = get_object_or_404(Submission.objects.exclude(status="removed"), pk=pk)
    if submission.user_id != request.user.pk and not request.user.is_staff:
        return HttpResponseForbidden()
    from .tasks import schedule_audit

    schedule_audit(pk)
    messages.success(request, "重新机审已排队 / Recheck queued")
    return redirect(
        "repositories:index"
        if submission.user_id == request.user.pk
        else "admin:repositories_submission_changelist"
    )
