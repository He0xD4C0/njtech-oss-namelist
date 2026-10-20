import hashlib
import hmac
import json
import secrets
from contextlib import nullcontext
from urllib.parse import urlsplit

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST
from django_ratelimit.decorators import ratelimit

from .forms import (
    CONFIG_GROUPS,
    BootstrapAccountForm,
    ConfigurationForm,
    DeleteAccountForm,
    ProfileForm,
    SMTPTestForm,
    group_title,
)
from .i18n import text
from .initialization import (
    SETUP_STEPS,
    STEP_GROUPS,
    SYNC_LABELS,
    TASK_KIND_LABELS,
    activation_checks,
    backup_status,
    recovery_label,
    wizard_tasks,
)
from .mail import mail_error, send_smtp_test
from .markdown import render_markdown
from .models import AuditEvent, BackupConfiguration, PolicyDocument, SiteState, SyncRun, User
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
    from .branding import LogoForm, save_logo
    if isinstance(form, LogoForm):
        return save_logo(form.cleaned_data["logo"], user)
    from .backup_forms import BackupConfigurationForm
    if isinstance(form, BackupConfigurationForm):
        from .backup import save_candidate
        return save_candidate(form.cleaned_data, user)
    with transaction.atomic():
        for key, value in form.cleaned_data.items():
            if key in SECRET_KEYS and not value:
                continue
            current = get_setting(key)
            if key == "smtp_security" and not current:
                current = "starttls"
            if value != current:
                set_setting(key, value, user=user)


def config_form(data=None, files=None, *, group, admin_style=False, complete=False):
    if group == "branding":
        from .branding import LogoForm
        return LogoForm(data, files, admin_style=admin_style)
    if group == "backup":
        from .backup_forms import BackupConfigurationForm
        return BackupConfigurationForm(data, admin_style=admin_style)
    return ConfigurationForm(data, group=group, admin_style=admin_style, complete=complete)


def backup_action(request, action):
    from .backup import queue_operation
    try:
        run = queue_operation(action.removeprefix("backup-"), request.user if request.user.is_authenticated else None)
    except (ValueError, BackupConfiguration.DoesNotExist) as error:
        messages.error(request, str(error) if isinstance(error, ValueError) else text("请先保存备份配置。", "Save backup settings first."))
    else:
        messages.info(request, text("任务已提交，请查看下方状态。", "Operation submitted. Check the status below.") if run.status != "queue-failed" else text("任务服务不可用，配置已保留，可重试。", "Task service unavailable. Settings are retained; retry later."))


def configuration_groups(data=None, selected=None, admin_style=False, keys=None, files=None):
    return [
        {"key": key, "title": group_title(key), "form": config_form(
            data if selected == key else None, files if selected == key else None, group=key, admin_style=admin_style
        )}
        for key, (title, fields) in CONFIG_GROUPS.items()
        if keys is None or key in keys
    ]


def queue_configuration_sync(request, action, recover_id=None):
    from .sync import queue_sync

    kind = action.removeprefix("sync-").removeprefix("recover-")
    try:
        run, queued = queue_sync(kind, recover_id=recover_id)
    except ValueError:
        messages.error(request, text("同步来源或任务已改变，请刷新状态后重试。", "The source or task changed. Refresh before retrying."))
        return
    if run.status == "queue-failed":
        messages.error(request, text("任务未能提交，请重试。", "Could not queue the task. Please retry."))
    elif recover_id is not None:
        messages.info(request, text("已重新排队，等待执行。", "Queued again; waiting for a worker.") if queued else text("任务状态已更新，请查看当前状态。", "Check the current task status."))
    return run


def setup_redirect(request, url):
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse({"redirect": url})
    return redirect(url)


def setup_render(request, context, status=200):
    if "application/json" in request.headers.get("Accept", ""):
        from .branding import logo_url
        return JsonResponse({"brand_logo_url": logo_url(), "html": render_to_string("core/setup_content.html", context, request=request),
                             "url": context.get("task", {}).get("url", "/setup/"),
                             "waiting": context.get("task", {}).get("waiting", False),
                             "revision": context.get("revision", "")}, status=status)
    return render(request, "core/setup.html", context, status=status)


@ratelimit(key="ip", rate="10/m", method="POST", block=True)
def authorize_setup(request, expected):
    if hmac.compare_digest(request.POST.get("secret", ""), settings.BOOTSTRAP_SECRET):
        request.session.cycle_key()
        request.session["bootstrap_authorization"] = expected
        return setup_redirect(request, "/setup/?step=" + request.session.get("setup_step", "mail"))
    return setup_render(request, {"authorization_error": True}, status=403)


@sensitive_post_parameters()
def setup(request):
    state, _ = SiteState.objects.get_or_create(pk=1)
    if state.activated_at:
        raise Http404
    if not settings.BOOTSTRAP_SECRET:
        return setup_render(request, {"bootstrap_unavailable": True}, status=503)
    expected = hashlib.sha256(settings.BOOTSTRAP_SECRET.encode()).hexdigest()
    authorized = hmac.compare_digest(request.session.get("bootstrap_authorization", ""), expected)
    action = request.POST.get("action", "")
    if request.method == "POST" and action == "authorize":
        return authorize_setup(request, expected)
    if not authorized:
        if request.GET.get("action") == "status":
            return JsonResponse({"authorized": False}, status=403)
        email_confirmed = bool(request.GET.get("confirmed") and state.bootstrap_user_id and EmailAddress.objects.filter(
            user_id=state.bootstrap_user_id, email__iexact=state.bootstrap_user.email, verified=True
        ).exists())
        return setup_render(request, {"authorized": False, "email_confirmed": email_confirmed})

    skipped = request.session.get("setup_skipped", [])
    tasks = wizard_tasks(state, skipped)
    from .backup import public_status
    backup = public_status()
    revision = json.dumps([(item["key"], item["ready"], item["run"].pk if item["run"] else None,
                            item["run"].status if item["run"] else None, item["recovery"]) for item in tasks] + [("backup-status", backup)])
    if request.method == "GET" and request.GET.get("action") == "status":
        return JsonResponse({"authorized": True, "revision": revision, "backup": backup,
                             "tasks": [{key: item[key] for key in ("key", "ready", "locked", "waiting", "recovery")} for item in tasks]})
    step = request.POST.get("step") or request.GET.get("step") or request.session.get("setup_step", "mail")
    if step not in STEP_GROUPS:
        step = "mail"
    requested = request.POST.get("task") or request.GET.get("task")
    group = request.POST.get("group")
    if group and (group not in CONFIG_GROUPS or group not in STEP_GROUPS[step]):
        return HttpResponseBadRequest("Unknown configuration group for this step")
    if not requested and group in CONFIG_GROUPS:
        requested = group
    if not requested and action in {"account", "test-smtp", "test-s3", "activate"}:
        requested = {"account": "admin", "test-smtp": "smtp", "test-s3": "storage", "activate": "review"}[action]
    if not requested and action in {"sync-documents", "sync-licenses"}:
        requested = action.removeprefix("sync-")
    candidates = [item for item in tasks if item["step"] == step]
    task = next((item for item in tasks if item["key"] == requested), None)
    task = task or next((item for item in candidates if not item["ready"] and not item["skipped"]), None) or (candidates[-1] if candidates else tasks[0])
    if task["locked"]:
        first = next(item for item in tasks if item["required"] and not item["ready"])
        if request.method == "POST":
            messages.error(request, text("请先完成前面的必需步骤。", "Complete the earlier required steps first."))
        return setup_redirect(request, first["url"])
    step = task["step"]
    request.session["setup_step"] = step
    key = task["key"]
    index = next(i for i, item in enumerate(tasks) if item["key"] == key)
    next_url = tasks[min(index + 1, len(tasks) - 1)]["url"]
    configuring = request.method == "POST" and action in {"configure", "test-smtp", "test-s3", "sync-documents", "sync-licenses", "backup-test"}
    form = config_form(request.POST if configuring else None, request.FILES if configuring else None, group=key, complete=True) if key in CONFIG_GROUPS else None
    recipient_form = SMTPTestForm(request.POST if action == "test-smtp" else None)
    account_form = BootstrapAccountForm(request.POST if action == "account" else None, request=request)
    if request.method == "POST":
        allowed = {"continue", "skip"}
        if form and key != "backup":
            allowed.add("configure")
        allowed.update({"smtp": {"test-smtp"}, "admin": {"account"},
                        "storage": {"test-s3"}, "documents": {"sync-documents", "recover-documents"},
                        "licenses": {"sync-licenses", "recover-licenses"}, "backup": {"backup-test", "backup-apply", "backup-backup"}, "review": {"activate"}}.get(key, set()))
        if action not in allowed or (group and group != key):
            return HttpResponseBadRequest("Unknown setup action or group")
        if action in {"backup-apply", "backup-backup"}:
            backup_action(request, action)
            return setup_redirect(request, task["url"])
        if action in {"recover-documents", "recover-licenses"}:
            try:
                run_id = int(request.POST.get("run_id", ""))
            except ValueError:
                return HttpResponseBadRequest("Invalid sync run")
            queue_configuration_sync(request, action, recover_id=run_id)
            return setup_redirect(request, task["url"])
        if action == "skip" and not task["required"]:
            request.session["setup_skipped"] = list(set(skipped) | {key})
            return setup_redirect(request, next_url)
        if action == "continue":
            if task["ready"] or not task["required"]:
                return setup_redirect(request, next_url)
            messages.error(request, text("请完成当前任务后继续。", "Complete this task before continuing."))
        if action == "backup-test" and form.is_valid():
            try:
                save_configuration(form)
            except ValueError as error:
                form.add_error(None, str(error))
            else:
                backup_action(request, action)
                return setup_redirect(request, task["url"])
        if configuring and key != "backup" and form.is_valid():
            recipient_valid = action != "test-smtp" or recipient_form.is_valid()
            if recipient_valid:
                save_configuration(form)
                if key in skipped:
                    request.session["setup_skipped"] = [value for value in skipped if value != key]
                if action == "test-smtp":
                    try:
                        if send_smtp_test(recipient_form.cleaned_data["recipient"] or form.cleaned_data["smtp_from_email"]) != 1:
                            raise RuntimeError("Message not accepted")
                        set_setting("smtp_verified", "1")
                    except Exception as exc:
                        form.add_error(None, text("配置已保存。", "Configuration saved. ") + mail_error(exc))
                    else:
                        messages.success(request, text("邮件服务器已接受测试邮件，请检查收件箱。现在可以创建管理员。", "The mail server accepted the test message. Check your inbox; you can now create the administrator."))
                        return setup_redirect(request, next_url)
                elif action in {"sync-documents", "sync-licenses"}:
                    queue_configuration_sync(request, action)
                    return setup_redirect(request, task["url"])
                elif action == "test-s3":
                    try:
                        from .avatar import test_storage
                        test_storage()
                        set_setting("s3_verified", "1")
                    except Exception:
                        form.add_error(None, text("配置已保存，但存储验证失败。请核对服务地址、桶名和访问权限后重试。", "Settings saved, but storage validation failed. Check the endpoint, bucket and permissions."))
                    else:
                        messages.success(request, text("头像存储的写入、回读和清理均已通过。", "Avatar storage write, readback and cleanup passed."))
                        return setup_redirect(request, next_url)
                else:
                    messages.success(request, text("配置已保存。", "Configuration saved."))
                    return setup_redirect(request, next_url if key not in {"smtp", "documents", "licenses", "storage", "backup", "branding"} else task["url"])
        elif action == "test-smtp":
            recipient_form.is_valid()
        if action == "account":
            if state.bootstrap_user_id:
                return setup_redirect(request, next_url)
            if account_form.is_valid():
                try:
                    from .email_verification import bind_verified_email, registration_proof

                    email = account_form.cleaned_data["email"]
                    with (registration_proof(request, email, "setup") if email else nullcontext()), transaction.atomic():
                        state = SiteState.objects.select_for_update().get(pk=1)
                        if state.bootstrap_user_id:
                            return setup_redirect(request, next_url)
                        if get_setting("smtp_verified") != "1":
                            return setup_redirect(request, tasks[0]["url"])
                        user = User.objects.create_superuser(username="admin", email=email,
                                                             password=account_form.cleaned_data["password"])
                        if email:
                            bind_verified_email(user.pk, email)
                        state.bootstrap_user = user
                        state.save(update_fields=["bootstrap_user"])
                    return setup_redirect(request, next_url)
                except Exception:
                    state.refresh_from_db()
                    account_form.add_error(None, text("账号未创建。请检查 admin 是否已存在或邮箱是否已绑定，然后重试。", "No account was created. Check whether admin or the email is already in use, then retry."))
        if action == "activate":
            with transaction.atomic():
                state = SiteState.objects.select_for_update().get(pk=1)
                if state.activated_at:
                    return setup_redirect(request, "/accounts/login/")
                checks = activation_checks(state)
                if all(check["ready"] for check in checks):
                    state.activated_at = timezone.now()
                    state.save(update_fields=["activated_at"])
                    AuditEvent.objects.create(actor=state.bootstrap_user, action="site.activated")
                    for session_key in ("bootstrap_authorization", "setup_step", "setup_skipped"):
                        request.session.pop(session_key, None)
                    messages.success(request, text("站点已激活，请使用管理员账号登录。", "Site activated. Sign in with your administrator account."))
                    return setup_redirect(request, "/accounts/login/")
            messages.error(request, text("仍有必需项未完成，请检查后重试。", "Required checks are incomplete. Review them and retry."))
    # Bound secret values are never rendered. Status polling never includes form fields.
    checks = activation_checks(state)
    descriptions = {
        "smtp": ("先确认站点能发出邮件，再创建管理员。", "Verify mail delivery before creating the administrator."),
        "admin": ("创建唯一超管 admin。邮箱可留空；如需绑定，请先验证邮箱再创建账号。", "Create the only superuser, admin. Email is optional; verify it before creating the account if supplied."),
        "turnstile": ("保护注册和登录入口，减少自动化滥用。", "Protect sign-up and sign-in against automated abuse."),
        "documents": ("从公开仓库同步隐私政策、服务条款和收录规则。至少需要一种语言的完整正文。", "Sync privacy, terms and inclusion policies from a public repository. At least one complete language is required."),
        "licenses": ("用于仓库许可证机审，可稍后配置。whitelist/ 与 blacklist/ 存放许可证正文，不是仓库 URL 名单。", "Optional license policy for repository review. whitelist/ and blacklist/ contain license texts, not lists of repository URLs."),
        "storage": ("为个人头像配置独立的对象存储，可稍后配置。", "Configure object storage for profile pictures, or do this later."),
        "branding": ("上传并应用站点 Logo；也可以保留默认标识，稍后在后台更换。", "Upload and apply a site logo, or keep the default and change it later in admin."),
        "backup": ("可选：保护数据库，可稍后配置。连接测试通过不等于已有备份。", "Optional database protection. You can configure it later; a connection test is not a backup."),
        "operations": ("通知邮箱、演练频率与外部备份是不同设置。这里不会替你启用服务器备份。", "Notifications, drill frequency and external backups are separate. This form does not enable server backups."),
        "review": ("确认必需项已完成，再开放站点。可选集成可以在管理后台继续配置。", "Review required checks before opening the site. Optional integrations can be configured later."),
    }

    description = text(*descriptions[key]) if key in descriptions else text(f"允许用户通过 {key.title()} 绑定平台账号；不需要时可以跳过。", f"Let users link their {key.title()} accounts, or skip this integration.")
    return setup_render(request, {
        "authorized": True, "step": step, "task": task, "tasks": tasks,
        "steps": [{"key": value, "title": text(zh, en), "number": i + 1,
                   "ready": value == "access" or all(item["ready"] or item["skipped"] for item in tasks if item["step"] == value),
                   "locked": value != "access" and all(item["locked"] for item in tasks if item["step"] == value)}
                  for i, (value, zh, en) in enumerate(SETUP_STEPS)],
        "step_number": next(i + 1 for i, item in enumerate(SETUP_STEPS) if item[0] == step),
        "description": description, "form": form, "recipient_form": recipient_form,
        "account_form": account_form, "site_state": state, "checks": checks,
        "can_activate": all(check["ready"] for check in checks), "email_verified": checks[1]["ready"],
        "previous_url": tasks[index - 1]["url"] if index else "",
        "next_url": next_url, "completed": [item for item in tasks[:index] if item["ready"] or item["skipped"]],
        "backup_status": text(*backup_status().split(" / ")), "backup": backup, "revision": revision,
        "provider_link": form.provider_link if form else "",
        "callback_url": form.callback_url if form else "",
    })


@login_required
@user_passes_test(lambda user: user.is_superuser)
@sensitive_post_parameters()
def configuration(request):
    from django.contrib import admin

    from .backup import public_status
    if request.method == "GET" and request.GET.get("action") == "backup-status":
        response = JsonResponse(public_status())
        response["Cache-Control"] = "no-store"
        return response
    action = request.POST.get("action")
    group = request.POST.get("group") if action in {"save", "backup-test", "test-smtp", "test-s3"} else None
    if request.method == "POST":
        allowed = {"save", "backup-test", "backup-apply", "backup-backup", "test-smtp", "test-s3",
                   "license-webhook", "policy-webhook", "sync-documents", "sync-licenses", "recover-documents", "recover-licenses"}
        expected_group = {"backup-test": "backup", "test-smtp": "smtp", "test-s3": "storage"}.get(action)
        if action not in allowed or (action in {"save", "backup-test"} and group not in CONFIG_GROUPS) or (
            expected_group and group and group != expected_group
        ):
            return HttpResponseBadRequest("无效的配置操作或分组")
    groups = configuration_groups(request.POST if group else None, group, admin_style=True, files=request.FILES if group else None)
    form = next((item["form"] for item in groups if item["key"] == group), None)
    new_secret = None
    recipient_form = SMTPTestForm(request.POST if request.POST.get("action") == "test-smtp" else None, admin_style=True)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "backup-test" and group == "backup" and form.is_valid():
            try:
                save_configuration(form, request.user)
            except ValueError as error:
                form.add_error(None, str(error))
            else:
                backup_action(request, action)
                return redirect("/manage/settings/#backup")
        if action in {"backup-apply", "backup-backup"}:
            backup_action(request, action)
            return redirect("/manage/settings/#backup")
        if action == "save" and form.is_valid():
            save_configuration(form, request.user)
            messages.success(request, "配置已保存。")
            return redirect("/manage/settings/#" + group)
        if action in ("license-webhook", "policy-webhook"):
            new_secret = secrets.token_urlsafe(48)
            key = "license_webhook_secret" if action == "license-webhook" else "policy_webhook_secret"
            set_setting(key, new_secret, user=request.user)
        if action == "test-smtp":
            try:
                valid = recipient_form.is_valid()
                if form is not None:
                    valid = form.is_valid() and valid
                if valid:
                    if form is not None:
                        save_configuration(form, request.user)
                    send_smtp_test(recipient_form.cleaned_data["recipient"] or get_setting("smtp_from_email"))
                    set_setting("smtp_verified", "1", user=request.user)
                    messages.success(request, "邮件服务器已接受测试邮件，请检查收件箱。")
                    return redirect("/manage/settings/#smtp")
            except Exception as exc:
                messages.error(request, mail_error(exc))
                return redirect("/manage/settings/#smtp")
        if action == "test-s3":
            try:
                from .avatar import test_storage

                if form is None or form.is_valid():
                    if form is not None:
                        save_configuration(form, request.user)
                    test_storage()
                    set_setting("s3_verified", "1", user=request.user)
                    messages.success(request, "头像存储写入、回读和清理测试通过。")
                    return redirect("/manage/settings/#storage")
            except Exception:
                messages.error(request, "头像存储测试失败，请检查服务地址、存储桶和访问凭据。")
                return redirect("/manage/settings/#storage")
        if action in ("recover-documents", "recover-licenses"):
            try:
                run_id = int(request.POST.get("run_id", ""))
            except ValueError:
                return HttpResponseBadRequest("无效的同步任务")
            queue_configuration_sync(request, action, recover_id=run_id)
            return redirect("/manage/settings/#task-status")
        if action in ("sync-documents", "sync-licenses"):
            run = queue_configuration_sync(request, action)
            if run and run.status != "queue-failed":
                messages.info(request, "同步请求已处理，请查看任务状态。")
            return redirect("/manage/settings/#task-status")
    from .sync import recovery_state
    sync_runs = list(SyncRun.objects.order_by("-created_at", "-pk")[:20])
    latest = {kind: SyncRun.objects.filter(kind=kind).order_by("-created_at", "-pk").values_list("pk", flat=True).first() for kind in ("documents", "licenses")}
    for run in sync_runs:
        state = recovery_state(run) if latest.get(run.kind) == run.pk else ""
        run.recovery_label = recovery_label(state)
        run.recoverable = state == "recoverable"
        run.kind_label = TASK_KIND_LABELS.get(run.kind, run.kind)
        run.status_label = SYNC_LABELS.get(run.status, (run.status,))[0]
    response = render(
        request,
        "core/configuration.html",
        {**admin.site.each_context(request), "title": "站点配置",
         "groups": groups, "new_secret": new_secret, "backup_status": text(*backup_status().split(" / ")),
         "sync_runs": sync_runs, "backup": public_status(), "recipient_form": recipient_form,
         "selected_group": group or {"license-webhook": "licenses", "policy-webhook": "documents", "test-smtp": "smtp", "test-s3": "storage"}.get(action, "smtp")},
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
        if request.user.is_superuser:
            form.add_error(None, text("唯一超管 admin 不能注销或转让。", "The only superuser, admin, cannot be deleted or transferred."))
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
    from .sync import queue_sync

    run, _ = queue_sync(kind)
    if run.status == "queue-failed":
        return JsonResponse({"status": "queue-failed"}, status=503)
    return JsonResponse({"status": "queued"}, status=202)
