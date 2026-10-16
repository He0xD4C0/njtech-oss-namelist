from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Exists, OuterRef
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from core.i18n import text

from .forms import RepositoryURLForm
from .models import AuditDispatch, Submission


@login_required
@ratelimit(key="user", rate="20/m", block=True)
def index(request):
    return render_index(request)


def render_index(request, *, form=None):
    from providers.api import ProviderUnavailable, RepositoryIneligible, get_client
    from providers.models import OAuthBinding

    platform = request.GET.get("platform", "")
    candidates = []
    bindings = OAuthBinding.objects.filter(user=request.user)
    binding = bindings.filter(platform=platform).first()
    candidate_error = ""
    submissions = list(request.user.submissions.exclude(status="removed")
                       .select_related("repository").annotate(review_pending=Exists(
                           AuditDispatch.objects.filter(submission_id=OuterRef("pk"),
                                                        generation=OuterRef("generation"),
                                                        completed_at__isnull=True)
                       )).order_by("-created_at"))
    retained = {(item.repository.platform, item.repository.platform_id) for item in submissions}
    if binding:
        try:
            candidates = get_client(platform, binding).list_owned_public()
        except (ProviderUnavailable, RepositoryIneligible):
            candidate_error = text(
                "暂时无法加载仓库列表。可以稍后重试，或尝试在上方粘贴仓库地址。",
                "Could not load your repositories. Try again later, or paste a repository URL above.",
            )
    else:
        platform = ""
    statuses = {
        "queued": text("等待检查", "Awaiting checks"),
        "needs_review": text("需要处理", "Action needed"),
        "published": text("已收录", "Listed"),
        "rejected": text("未通过检查", "Checks not passed"),
        "historical": text("已停止展示", "No longer listed"),
    }
    hints = {
        "queued": text("系统将自动检查仓库归属和许可证，通过后会显示在目录中。",
                       "Ownership and license checks run automatically. Your repository appears in the directory once approved."),
        "needs_review": text("暂时无法完成检查。请查看详情，处理后重新检查；站点配置问题请联系管理员。",
                             "Checks could not finish. Review the details and recheck after resolving the issue; contact an administrator for site configuration issues."),
        "published": text("仓库已在目录中公开展示。", "Your repository is visible in the directory."),
        "rejected": text("请查看检查详情和收录规则，修改仓库后点击“重新检查”。",
                         "Review the details and inclusion rules, update your repository, then choose Recheck."),
        "historical": text("当前未在目录中展示。恢复收录条件后，可以重新检查。",
                           "This repository is no longer listed. Recheck once it meets the inclusion rules again."),
    }
    for submission in submissions:
        submission.status_label = statuses[submission.status]
        submission.status_hint = hints[submission.status]
        zh_reason, separator, en_reason = submission.reason.partition(" / ")
        submission.display_reason = text(zh_reason, en_reason) if separator else submission.reason
    return render(request, "repositories/manage.html", {
        "form": form if form is not None else RepositoryURLForm(),
        "submissions": submissions,
        "quota_reached": len(submissions) >= request.user.quota,
        "bindings": bindings,
        "candidates": [{"repo": repo, "submitted": (repo.platform, repo.platform_id) in retained}
                       for repo in candidates],
        "candidate_error": candidate_error,
        "platform": platform,
    })


@login_required
@require_POST
@ratelimit(key="user", rate="10/m", block=True)
def add(request):
    from providers.api import ProviderUnavailable, RepositoryIneligible
    from providers.models import OAuthBinding

    from .services import add_submission

    form = RepositoryURLForm(request.POST) if "repository_url" in request.POST else None
    if form is not None and not form.is_valid():
        return render_index(request, form=form)
    try:
        if form is not None:
            add_submission(request.user, repository_url=form.cleaned_data["repository_url"])
        else:
            add_submission(request.user, request.POST.get("platform", ""), request.POST.get("repository_id", ""))
    except ValueError as error:
        error_message = str(error)
    except OAuthBinding.DoesNotExist:
        error_message = text(
            "请先绑定这个仓库所在的平台账号，再返回提交。可以通过下方“绑定平台账号”继续。",
            "Connect your account on this repository's platform first, then return to submit. Use Connect a platform account below.",
        )
    except RepositoryIneligible:
        error_message = text(
            "无法提交这个仓库。请确认地址正确、仓库公开，且由你绑定的个人账号拥有；组织或他人的仓库暂不支持。",
            "Check that the URL is correct and the repository is public and owned by your connected personal account. Organization repositories and other owners are not supported.",
        )
    except ProviderUnavailable:
        error_message = text(
            "暂时无法连接仓库平台，本次未提交成功。请稍后重试；若持续失败，请到平台绑定页面重新授权。",
            "The repository platform is temporarily unavailable; nothing was submitted. Try again later, or reauthorize on the platform connections page if the problem persists.",
        )
    else:
        messages.success(request, text(
            "提交成功，正在等待自动检查。通过后会展示在目录中，你可以在下方查看状态。",
            "Repository submitted and awaiting automatic checks. It will appear in the directory once approved. Check its status below.",
        ))
        return redirect(reverse("repositories:index") + "#submissions")
    if form is not None:
        form.add_error("repository_url", error_message)
        return render_index(request, form=form)
    messages.error(request, error_message)
    return redirect("repositories:index")


@login_required
@require_POST
def remove(request, pk):
    submission = get_object_or_404(Submission, pk=pk, user=request.user)
    from .services import remove_submission

    remove_submission(submission, request.user)
    messages.success(request, text("已从本站移除，平台上的原仓库不受影响。",
                                   "Entry removed from this site. Your original repository is unchanged."))
    return redirect("repositories:index")


@login_required
@require_POST
@ratelimit(key="user", rate="10/m", block=True)
def recheck(request, pk):
    submission = get_object_or_404(Submission.objects.exclude(status="removed"), pk=pk)
    if submission.user_id != request.user.pk and not request.user.is_staff:
        return HttpResponseForbidden()
    from .tasks import schedule_audit

    if submission.dispatches.filter(generation=submission.generation, completed_at__isnull=True).exists():
        messages.info(request, text("检查已在排队或进行中，无需重复提交。", "Checks are already queued or running."))
    else:
        schedule_audit(pk)
        messages.success(request, text("已安排重新检查，请稍后刷新查看结果。", "Recheck requested. Refresh later for results."))
    return redirect(
        "repositories:index"
        if submission.user_id == request.user.pk
        else "admin:repositories_submission_changelist"
    )
