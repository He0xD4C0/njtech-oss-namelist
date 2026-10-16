from allauth.account.adapter import DefaultAccountAdapter

from .models import PolicyConsent, username_validator
from .runtime import get_setting, registration_ready


class AccountAdapter(DefaultAccountAdapter):
    def render_mail(self, template_prefix, email, context, headers=None):
        from .i18n import text
        from .mail import style_email

        message = super().render_mail(template_prefix, email, context, headers)
        url = context.get("activate_url") or context.get("password_reset_url") or ""
        label = text("确认此邮箱", "Confirm email") if context.get("activate_url") else text("重置密码", "Reset password")
        if context.get("activate_url"):
            message.subject = self.format_email_subject(text("请确认你的邮箱", "Confirm your email"))
            message.body = text(
                "你正在为 NJTech OSS 账号确认邮箱。请点击下方按钮完成确认，然后返回原页面继续。\n\n如果这不是你的操作，请忽略此邮件。",
                "Please confirm this email address for your NJTech OSS account using the button below, then return to the original page to continue.\n\nIf you did not request this, you can ignore this email.",
            )
        elif context.get("password_reset_url"):
            message.subject = self.format_email_subject(text("重置你的密码", "Reset your password"))
            message.body = text(
                "我们收到了你的账号密码重置请求。请点击下方按钮设置新密码。\n\n如果这不是你的操作，请忽略此邮件；你的密码不会因此改变。",
                "We received a password reset request for your account. Use the button below to choose a new password.\n\nIf you did not request this, ignore this email; your password will remain unchanged.",
            )
            if context.get("username"):
                message.body += "\n\n" + text("用户名：", "Username: ") + str(context["username"])
        return style_email(message, action_url=url, action_label=label)

    def get_email_verification_redirect_url(self, email_address):
        from .models import SiteState

        if SiteState.objects.filter(
            pk=1, activated_at__isnull=True, bootstrap_user_id=email_address.user_id
        ).exists():
            return "/setup/?step=mail&confirmed=1"
        return super().get_email_verification_redirect_url(email_address)

    def is_open_for_signup(self, request):
        return registration_ready()

    def clean_username(self, username, shallow=False):
        username_validator(username)
        if username == "admin":
            from django.core.exceptions import ValidationError

            raise ValidationError("admin 为保留用户名 / admin is reserved")
        return super().clean_username(username, shallow=shallow)

    def get_login_stages(self):
        return ["core.authentication.VerifiedMailboxStage" if stage == "allauth.account.stages.EmailVerificationStage"
                else stage for stage in super().get_login_stages()]

    def is_email_verified(self, request, email):
        from .email_verification import proven

        return proven(request, email, "signup") or proven(request, email, "social")

    def get_from_email(self):
        return get_setting("smtp_from_email") or super().get_from_email()

    def set_password(self, user, password):
        # A concurrent mailbox/role change must not be overwritten by allauth's
        # otherwise unrestricted save of the user loaded at reset initiation.
        user.set_password(password)
        user.save(update_fields=["password"])

    def save_user(self, request, user, form, commit=True):
        user = super().save_user(request, user, form, commit=commit)
        if commit and form.cleaned_data.get("policy_language"):
            PolicyConsent.objects.create(
                user=user,
                language=form.cleaned_data["policy_language"],
                versions=form.cleaned_data["policy_versions"],
            )
        return user
