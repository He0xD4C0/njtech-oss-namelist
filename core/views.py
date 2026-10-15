import hashlib
import hmac
import json
import secrets
from urllib.parse import urlsplit

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.mail import send_mail
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST
from django_ratelimit.decorators import ratelimit

from .forms import CONFIG_GROUPS, BootstrapAccountForm, ConfigurationForm, DeleteAccountForm, ProfileForm
from .initialization import SETUP_STEPS, STEP_GROUPS, activation_checks, backup_status
from .markdown import render_markdown
from .models import AuditEvent, PolicyDocument, SiteState, SyncRun, User
from .runtime import SECRET_KEYS, get_setting, set_setting


def health(request):
    from django.db import connection

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return JsonResponse({"status": "ok"})
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)


def save_configuration(form, user=None):
    with transaction.atomic():
        for key, value in form.cleaned_data.items():
            if key in SECRET_KEYS and not value:
                continue
            current = get_setting(key)
            if key == "smtp_security" and not current:
                current = "starttls"
            if value != current:
                set_setting(key, value, user=user)


def configuration_groups(data=None, selected=None, admin_style=False, keys=None):
    return [
        {"key": key, "title": title, "form": ConfigurationForm(
            data if selected == key else None, group=key, admin_style=admin_style
        )}
        for key, (title, fields) in CONFIG_GROUPS.items()
        if keys is None or key in keys
    ]


def queue_configuration_sync(request, action):
    from .tasks import sync_documents, sync_license_policy

    documents = action == "sync-documents"
    if not get_setting("policy_source_url" if documents else "license_source_url"):
        messages.error(request, "请先保存同步来源。 / Save the source before queuing a sync.")
        return
    run = SyncRun.objects.create(kind="documents" if documents else "licenses", status="queued", message="同步请求已提交，完成结果另行记录。 / Sync requested; completion is recorded separately.")
    try:
        (sync_documents if action == "sync-documents" else sync_license_policy).delay()
    except Exception:
        run.status = "queue-failed"
        run.message = "任务提交失败，可重试。 / Task submission failed; retry is available."
        run.save(update_fields=["status", "message"])
        messages.error(request, "同步任务未能提交，请检查任务服务后重试。 / Could not queue sync; check the worker service and retry.")
    else:
        messages.info(request, "同步任务已提交；请刷新查看实际结果。 / Sync queued; refresh to check the actual result.")


@ratelimit(key="ip", rate="10/m", method="POST", block=True)
def authorize_setup(request, expected):
    if hmac.compare_digest(request.POST.get("secret", ""), settings.BOOTSTRAP_SECRET):
        request.session.cycle_key()
        request.session["bootstrap_authorization"] = expected
        return redirect("/setup/?step=" + request.session.get("setup_step", "mail"))
    return render(request, "core/setup.html", {"authorization_error": True}, status=403)


@sensitive_post_parameters()
def setup(request):
    state, _ = SiteState.objects.get_or_create(pk=1)
    if state.activated_at:
        raise Http404
    if not settings.BOOTSTRAP_SECRET:
        return render(request, "core/setup.html", {"bootstrap_unavailable": True}, status=503)
    expected = hashlib.sha256(settings.BOOTSTRAP_SECRET.encode()).hexdigest()
    authorized = hmac.compare_digest(request.session.get("bootstrap_authorization", ""), expected)
    action = request.POST.get("action", "")
    if request.method == "POST" and action == "authorize":
        return authorize_setup(request, expected)
    if not authorized:
        email_confirmed = bool(request.GET.get("confirmed") and state.bootstrap_user_id and EmailAddress.objects.filter(
            user_id=state.bootstrap_user_id, email__iexact=state.bootstrap_user.email, verified=True
        ).exists())
        return render(request, "core/setup.html", {"authorized": False, "email_confirmed": email_confirmed})

    step = request.POST.get("step") or request.GET.get("step") or request.session.get("setup_step", "mail")
    if step not in STEP_GROUPS:
        step = "mail"
    request.session["setup_step"] = step
    group = request.POST.get("group") if action == "configure" else None
    if request.method == "POST" and action == "configure" and group not in STEP_GROUPS[step]:
        return HttpResponseBadRequest("Unknown configuration group")
    groups = configuration_groups(request.POST if action == "configure" else None, group, keys=STEP_GROUPS[step])
    selected_form = next((item["form"] for item in groups if item["key"] == group), None)
    account_form = BootstrapAccountForm(request.POST if action == "account" else None)
    if request.method == "POST":
        if action == "configure" and selected_form.is_valid():
            save_configuration(selected_form)
            messages.success(request, "本组配置已保存 / Configuration group saved")
            return redirect("/setup/?step=" + step)
        if action in ("sync-documents", "sync-licenses"):
            queue_configuration_sync(request, action)
            return redirect("/setup/?step=requirements")
        if action == "account" and not state.bootstrap_user_id and account_form.is_valid():
            if not get_setting("smtp_host"):
                account_form.add_error(None, "请先保存 SMTP 配置 / Save SMTP configuration first")
            else:
                try:
                    with transaction.atomic():
                        state = SiteState.objects.select_for_update().get(pk=1)
                        if state.bootstrap_user_id:
                            return redirect("/setup/?step=mail")
                        user = User.objects.create_user(
                            username=account_form.cleaned_data["username"],
                            email=account_form.cleaned_data["email"],
                            password=account_form.cleaned_data["password"],
                        )
                        state.bootstrap_user = user
                        state.save(update_fields=["bootstrap_user"])
                        address = EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=False)
                        address.send_confirmation(request, signup=True)
                        set_setting("smtp_verified", "1")
                    messages.success(request, "验证邮件已发送，请在邮箱中确认后返回此步骤。 / Verification sent; confirm your email and return here.")
                    return redirect("/setup/?step=mail")
                except Exception:
                    state.refresh_from_db()
                    account_form.add_error(None, "账号或邮件创建失败。请检查 SMTP 配置与账号信息后重试；账号未创建。 / Account or mail creation failed. Check SMTP and account details, then retry; no account was created.")
        if action in ("resend", "test-smtp") and state.bootstrap_user_id:
            try:
                if action == "resend":
                    EmailAddress.objects.get(user=state.bootstrap_user, primary=True).send_confirmation(request, signup=True)
                else:
                    send_mail("OSS Directory SMTP test", "SMTP connection and delivery test.", None, [state.bootstrap_user.email])
                set_setting("smtp_verified", "1")
                messages.success(request, "邮件已发送 / Email sent")
            except Exception:
                messages.error(request, "邮件发送失败，请检查 SMTP 后重试。 / Email delivery failed; check SMTP and retry.")
            return redirect("/setup/?step=mail")
        if action == "test-s3":
            try:
                from .avatar import test_storage

                test_storage()
                set_setting("s3_verified", "1")
                messages.success(request, "头像存储写入、回读和清理通过 / Avatar storage write, readback and cleanup passed")
            except Exception:
                messages.error(request, "头像存储验证失败，请检查配置后重试。 / Avatar storage verification failed; check configuration and retry.")
            return redirect("/setup/?step=integrations")
        if action == "activate":
            with transaction.atomic():
                state = SiteState.objects.select_for_update().get(pk=1)
                if state.activated_at:
                    return redirect("account_login")
                checks = activation_checks(state)
                if all(check["ready"] for check in checks):
                    User.objects.filter(pk=state.bootstrap_user_id).update(is_staff=True, is_superuser=True)
                    state.activated_at = timezone.now()
                    state.save(update_fields=["activated_at"])
                    AuditEvent.objects.create(actor=state.bootstrap_user, action="site.activated")
                    request.session.pop("bootstrap_authorization", None)
                    request.session.pop("setup_step", None)
                    messages.success(request, "站点已激活，请使用初始管理员账号登录。 / Site activated; sign in with the initial administrator account.")
                    return redirect("account_login")
            messages.error(request, "仍有必需项未完成，请按检查清单返回对应步骤。 / Required checks are incomplete; return to the indicated steps.")
            step = "review"
        if action not in {"configure", "account", "resend", "test-smtp", "activate"}:
            return HttpResponseBadRequest("Unknown setup action")
    checks = activation_checks(state)
    position = next(index for index, item in enumerate(SETUP_STEPS) if item[0] == step)
    return render(request, "core/setup.html", {
        "authorized": True, "step": step, "steps": SETUP_STEPS, "step_number": position + 1,
        "next_step": SETUP_STEPS[position + 1][0] if position < len(SETUP_STEPS) - 1 else None,
        "previous_step": SETUP_STEPS[position - 1][0] if position > 0 else None,
        "groups": [item for item in groups if item["key"] in STEP_GROUPS[step]],
        "account_form": account_form, "site_state": state, "checks": checks,
        "can_activate": all(check["ready"] for check in checks), "email_verified": checks[1]["ready"],
        "backup_status": backup_status(),
        "sync_runs": SyncRun.objects.filter(kind__in=("documents", "licenses")).order_by("-created_at")[:10],
    })


@login_required
@user_passes_test(lambda user: user.is_superuser)
@sensitive_post_parameters()
def configuration(request):
    from django.contrib import admin

    group = request.POST.get("group") if request.POST.get("action") == "save" else None
    if request.method == "POST" and request.POST.get("action") == "save" and group not in CONFIG_GROUPS:
        return HttpResponseBadRequest("Unknown configuration group")
    groups = configuration_groups(request.POST if group else None, group, admin_style=True)
    form = next((item["form"] for item in groups if item["key"] == group), None)
    new_secret = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save" and form.is_valid():
            save_configuration(form, request.user)
            messages.success(request, "配置已保存 / Configuration saved")
            return redirect("/manage/settings/#" + group)
        if action in ("license-webhook", "policy-webhook"):
            new_secret = secrets.token_urlsafe(48)
            key = "license_webhook_secret" if action == "license-webhook" else "policy_webhook_secret"
            set_setting(key, new_secret, user=request.user)
        if action == "test-smtp":
            try:
                send_mail(
                    "OSS Directory SMTP test",
                    "SMTP connection and delivery test.",
                    None,
                    [request.user.email],
                )
                set_setting("smtp_verified", "1", user=request.user)
                messages.success(request, "测试邮件已发送 / Test mail sent")
            except Exception:
                messages.error(request, "SMTP 测试失败 / SMTP test failed")
            return redirect("core:configuration")
        if action == "test-s3":
            try:
                from .avatar import test_storage

                test_storage()
                set_setting("s3_verified", "1", user=request.user)
                messages.success(request, "S3 写入、回读和清理通过 / S3 write, readback and cleanup passed")
            except Exception:
                messages.error(request, "S3 测试失败 / S3 test failed")
            return redirect("core:configuration")
        if action in ("sync-documents", "sync-licenses"):
            queue_configuration_sync(request, action)
            return redirect("core:configuration")
    response = render(
        request,
        "core/configuration.html",
        {**admin.site.each_context(request), "title": "运行时配置 / Runtime configuration",
         "groups": groups, "new_secret": new_secret, "backup_status": backup_status(),
         "sync_runs": SyncRun.objects.order_by("-created_at")[:20]},
    )
    response["Cache-Control"] = "no-store"
    return response


@login_required
def profile(request):
    form = ProfileForm(request.POST or None, request.FILES or None, instance=request.user)
    if request.method == "POST" and form.is_valid():
        avatar = form.cleaned_data.get("avatar")
        if avatar:
            try:
                from .avatar import upload_avatar

                if avatar.size > 5 * 1024 * 1024:
                    raise ValueError("头像不能超过 5 MiB / Avatar must not exceed 5 MiB")
                upload_avatar(request.user, avatar.read())
            except Exception:
                form.add_error("avatar", "头像存储或图片处理失败 / Avatar storage or processing failed")
        if not form.errors:
            form.save()
            messages.success(request, "资料已更新 / Profile updated")
            return redirect("core:profile")
    return render(request, "core/profile.html", {"form": form})


@login_required
def delete_account(request):
    form = DeleteAccountForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        # Preserve at least one functioning superadmin for configuration recovery.
        if (
            request.user.is_superuser
            and not User.objects.filter(is_superuser=True, is_active=True)
            .exclude(pk=request.user.pk)
            .exists()
        ):
            form.add_error(None, "请先指定其他超管 / Assign another superadmin before deleting this account")
        else:
            from .services import delete_account as perform_deletion

            perform_deletion(request.user)
            logout(request)
            return redirect("directory:home")
    return render(request, "core/delete_account.html", {"form": form})


@require_GET
def policy(request, kind):
    if kind not in {"privacy", "terms", "inclusion"}:
        raise Http404
    ui_language = "en-US" if request.LANGUAGE_CODE.startswith("en") else "zh-CN"
    language = request.GET.get("language", ui_language)
    document = PolicyDocument.objects.filter(kind=kind, language=language).first()
    available = list(PolicyDocument.objects.filter(kind=kind).values_list("language", flat=True))
    text_replacements = {
        "{{ operator_name }}": get_setting("operator_name") or (
            "Site operator" if language == "en-US" else "本站运营者"
        ),
        "{{ operator_contact_email }}": get_setting("operator_contact_email") or (
            "Contact email not set" if language == "en-US" else "联系邮箱未设置"
        ),
    }
    return render(
        request,
        "core/policy.html",
        {
            "document": document,
            "body_html": render_markdown(document.body, text_replacements=text_replacements) if document else "",
            "available_languages": available,
            "selected_language": language,
            "kind": kind,
        },
    )


@login_required
@require_POST
def markdown_preview(request):
    if not request.user.is_staff:
        return HttpResponseForbidden()
    if len(request.body) > 1_048_576:
        return JsonResponse({"error": "Content too large"}, status=413)
    return JsonResponse({"html": str(render_markdown(request.POST.get("body", "")))})


@csrf_exempt
@require_POST
@ratelimit(key="ip", rate="60/m", method="POST", block=True)
def webhook(request, kind):
    if kind not in ("licenses", "documents") or len(request.body) > 1_048_576:
        return HttpResponse(status=400)
    secret = get_setting("license_webhook_secret" if kind == "licenses" else "policy_webhook_secret")
    source = get_setting("license_source_url" if kind == "licenses" else "policy_source_url")
    if not secret or not source:
        return HttpResponse(status=404)
    host = urlsplit(source).hostname
    if host == "github.com":
        expected = "sha256=" + hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
        valid = hmac.compare_digest(expected, request.headers.get("X-Hub-Signature-256", ""))
        event = request.headers.get("X-GitHub-Event")
    elif host == "gitlab.com":
        valid = hmac.compare_digest(secret, request.headers.get("X-Gitlab-Token", ""))
        event = request.headers.get("X-Gitlab-Event")
    else:
        valid = hmac.compare_digest(secret, request.headers.get("X-Gitee-Token", ""))
        event = request.headers.get("X-Gitee-Event")
    if not valid:
        return HttpResponseForbidden()
    if event not in ("push", "Push Hook"):
        return JsonResponse({"status": "ignored"})
    try:
        payload = json.loads(request.body)
        repository = payload.get("project", {}) if host == "gitlab.com" else payload.get("repository", {})
        actual = repository.get("html_url") or repository.get("web_url") or repository.get("homepage")
        from .git import validate_git_url

        if not actual or validate_git_url(actual) != source:
            return HttpResponseForbidden()
        default_branch = repository.get("default_branch")
        if not default_branch or payload.get("ref") != f"refs/heads/{default_branch}":
            return JsonResponse({"status": "ignored"})
    except (ValueError, TypeError):
        return HttpResponse(status=400)
    from .tasks import sync_documents, sync_license_policy

    (sync_license_policy if kind == "licenses" else sync_documents).delay()
    return JsonResponse({"status": "queued"}, status=202)
