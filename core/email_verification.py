"""Mailbox proofs without persisting pending addresses or registration passwords."""
import hashlib
import hmac
import json
import secrets
from contextlib import contextmanager

from allauth.account import app_settings
from cryptography.fernet import InvalidToken
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.crypto import salted_hmac
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from .crypto import cipher
from .i18n import text
from .models import User

RETURNS = {"signup": "account_signup", "social": "socialaccount_signup", "add": "account_email",
           "recover": "account_email_verification_sent", "setup": "core:setup"}


def ttl():
    return app_settings.EMAIL_CONFIRMATION_EXPIRE_DAYS * 86400


def email_digest(email):
    return salted_hmac("mailbox-proof", email.lower().strip()).hexdigest()


def owner(request):
    value = request.session.get("mailbox_owner")
    if not value:
        value = secrets.token_urlsafe(24)
        request.session["mailbox_owner"] = value
    return value


def active_key(owner_id, purpose):
    return f"mailbox:active:{owner_id}:{purpose}"


def state_key(nonce):
    return f"mailbox:proof:{nonce}"


def current(request, purpose):
    nonce = cache.get(active_key(owner(request), purpose))
    state = cache.get(state_key(nonce)) if nonce else None
    return nonce, state


def proven(request, email, purpose):
    _, state = current(request, purpose)
    return bool(state and state["status"] == "verified" and state["digest"] == email_digest(email))


@contextmanager
def proof_lock(nonce):
    key = f"mailbox:lock:{nonce}"
    if not cache.add(key, True, timeout=60):
        raise ValidationError(text("操作正在进行，请稍后重试。", "An operation is in progress. Try again shortly."))
    try:
        yield
    finally:
        cache.delete(key)


@contextmanager
def registration_proof(request, email, purpose):
    nonce, _ = current(request, purpose)
    with proof_lock(nonce), transaction.atomic():
        if not proven(request, email, purpose):
            raise ValidationError(text("请先验证此邮箱，然后重新提交。", "Verify this email before submitting."))
        yield
        transaction.on_commit(lambda: cache.delete(state_key(nonce)))


def recovery_user(request):
    from allauth.account.stages import LoginStageController

    stage = LoginStageController.enter(request, "verify_email")
    if stage and stage.login.user and stage.login.user.is_active:
        return stage.login.user
    return None


def authorize_purpose(request, purpose):
    from .runtime import registration_ready

    if purpose == "add" and request.user.is_authenticated:
        return request.user.pk
    if purpose == "recover":
        user = recovery_user(request)
        if user:
            return user.pk
    if purpose in {"signup", "social"} and not request.user.is_authenticated and registration_ready():
        if purpose == "signup" or request.session.get("socialaccount_sociallogin"):
            return None
    if purpose == "setup" and settings.BOOTSTRAP_SECRET:
        from .models import SiteState

        expected = hashlib.sha256(settings.BOOTSTRAP_SECRET.encode()).hexdigest()
        if hmac.compare_digest(request.session.get("bootstrap_authorization", ""), expected):
            if SiteState.objects.filter(pk=1, activated_at__isnull=True).exists():
                return None
    raise PermissionDenied


@never_cache
@require_POST
@ratelimit(key="ip", rate="20/h", method="POST", block=True)
def request_verification(request):
    from allauth.account.internal.flows.email_verification import handle_verification_email_rate_limit

    from .mail import mail_error, send_system_mail
    from .turnstile import verify_turnstile

    purpose = request.POST.get("purpose", "")
    user_id = authorize_purpose(request, purpose)
    if request.POST.get("action") == "cancel":
        nonce, _ = current(request, purpose)
        cache.delete(state_key(nonce))
        cache.delete(active_key(owner(request), purpose))
        return JsonResponse({"status": "expired"})
    try:
        email = forms.EmailField().clean(request.POST.get("email", "")).lower()
        if purpose in {"signup", "social"}:
            verify_turnstile(request)
        if not handle_verification_email_rate_limit(request, email):
            return JsonResponse({"error": text("发送过于频繁，请稍后重试。", "Please wait before requesting another email.")}, status=429)
    except ValidationError as exc:
        return JsonResponse({"error": " ".join(exc.messages)}, status=400)
    nonce, owner_id = secrets.token_urlsafe(32), owner(request)
    state = {"owner": owner_id, "purpose": purpose, "user_id": user_id,
             "digest": email_digest(email), "status": "pending"}
    previous, _ = current(request, purpose)
    cache.set(state_key(nonce), state, ttl())
    cache.set(active_key(owner_id, purpose), nonce, ttl())
    if previous:
        cache.delete(state_key(previous))
    payload = {"nonce": nonce, "email": email, **state}
    key = cipher().encrypt(json.dumps(payload).encode()).decode()
    url = settings.SITE_BASE_URL.rstrip("/") + reverse("account_confirm_email", args=[key])
    try:
        sent = send_system_mail(
            text("NJTech OSS · 请确认邮箱", "NJTech OSS · Confirm your email"),
            text("请确认你正在本站发起的邮箱验证，完成后返回原页面继续。若非本人操作，请忽略。",
                 "Confirm the email verification you requested, then return to the original page. Ignore this if you did not request it."),
            [email], action_url=url, action_label=text("确认邮箱", "Confirm email"),
        )
        if sent != 1:
            raise OSError("Mail not accepted")
    except Exception as exc:
        cache.delete(state_key(nonce))
        return JsonResponse({"error": mail_error(exc)}, status=400)
    return JsonResponse({"status": "pending"})


def decode(key):
    try:
        payload = json.loads(cipher().decrypt(key.encode(), ttl=ttl()))
        nonce = payload["nonce"]
        state = cache.get(state_key(nonce))
        if (not state or state["owner"] != payload["owner"] or state["purpose"] != payload["purpose"]
                or state["user_id"] != payload["user_id"] or state["digest"] != email_digest(payload["email"])
                or cache.get(active_key(state["owner"], state["purpose"])) != nonce):
            return None, None
        return payload, state
    except (InvalidToken, ValueError, KeyError, TypeError):
        return None, None


@never_cache
@require_POST
def verification_status(request):
    purpose = request.POST.get("purpose", "")
    authorize_purpose(request, purpose)
    _, state = current(request, purpose)
    return JsonResponse({"status": state["status"] if state else "expired",
                         "matches": bool(state and state["digest"] == email_digest(request.POST.get("email", "")))})


def bind_verified_email(user_id, email):
    """Caller holds a transaction; serialize all mailbox changes on the user."""
    from allauth.account.models import EmailAddress

    user = User.objects.select_for_update().get(pk=user_id, is_active=True)
    existing = EmailAddress.objects.filter(email__iexact=email).first()
    if existing:
        if existing.user_id != user.pk:
            raise ValidationError(text("此邮箱已绑定其他账户。", "This email is already bound to another account."))
        return existing
    primary = not EmailAddress.objects.filter(user=user, primary=True).exists()
    address = EmailAddress.objects.create(user=user, email=email.lower(), verified=True, primary=primary)
    if primary:
        user.email = address.email
        user.save(update_fields=["email"])
    return address


@never_cache
@require_http_methods(["GET", "POST"])
def confirm_email(request, key):
    payload, state = decode(key)
    error = ""
    target = None
    if payload and state["user_id"]:
        target = User.objects.filter(pk=state["user_id"], is_active=True).first()
        if not target:
            payload = state = None
    if payload and request.method == "POST" and state["status"] == "pending":
        try:
            with proof_lock(payload["nonce"]):
                payload, state = decode(key)
                if not payload:
                    raise ValidationError(text("验证请求已失效。", "This verification request has expired."))
                with transaction.atomic():
                    if state["user_id"]:
                        bind_verified_email(state["user_id"], payload["email"])
                    state["status"] = "verified"
                    transaction.on_commit(lambda: cache.set(state_key(payload["nonce"]), state, ttl()))
        except (IntegrityError, ValidationError, User.DoesNotExist) as exc:
            error = " ".join(exc.messages) if isinstance(exc, ValidationError) else text(
                "邮箱无法绑定，可能已被其他账户绑定。请返回原页面重试。",
                "This email could not be bound. It may belong to another account. Return and try again.")
    return render(request, "account/email_confirm.html", {
        "proof_email": payload["email"] if payload else "", "target_user": target,
        "confirmed": bool(state and state["status"] == "verified"), "error": error,
        "return_url": reverse(RETURNS[state["purpose"]]) if state else reverse("account_login"),
    })


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def manage_emails(request):
    from allauth.account.models import EmailAddress

    if request.method == "POST":
        try:
            with transaction.atomic():
                user = User.objects.select_for_update().get(pk=request.user.pk)
                address = EmailAddress.objects.get(user=user, email__iexact=request.POST.get("email", ""))
                if "action_primary" in request.POST:
                    EmailAddress.objects.filter(user=user, primary=True).update(primary=False)
                    address.primary = True
                    address.save(update_fields=["primary"])
                    user.email = address.email
                    user.save(update_fields=["email"])
                elif "action_remove" in request.POST:
                    others = EmailAddress.objects.filter(user=user).exclude(pk=address.pk).exists()
                    if address.primary and others:
                        raise ValidationError(text("请先设置其他默认邮箱。", "Choose another default email first."))
                    if not others and not user.is_superuser:
                        raise ValidationError(text("必须保留至少一个已验证邮箱。", "Keep at least one verified email."))
                    address.delete()
                    if address.primary:
                        user.email = ""
                        user.save(update_fields=["email"])
                else:
                    raise ValidationError(text("无效操作。", "Invalid action."))
            messages.success(request, text("邮箱设置已更新。", "Email settings updated."))
        except (EmailAddress.DoesNotExist, ValidationError, IntegrityError) as exc:
            messages.error(request, " ".join(exc.messages) if isinstance(exc, ValidationError) else text(
                "邮箱状态已变化，请刷新后重试。", "Email state changed. Refresh and try again."))
        return redirect("account_email")
    return render(request, "account/email.html", {
        "emailaddresses": EmailAddress.objects.filter(user=request.user).order_by("-primary", "pk"),
    })


@never_cache
@require_GET
def recovery(request):
    from allauth.account.internal.stagekit import clear_login
    from allauth.account.models import EmailAddress

    user = recovery_user(request)
    if request.GET.get("cancel") == "1" or (user and EmailAddress.objects.filter(user=user, verified=True).exists()):
        clear_login(request)
        return redirect("account_login")
    if not user:
        return redirect("account_email" if request.user.is_authenticated else "account_login")
    return render(request, "account/verification_sent.html", {"recovery_user": user})
