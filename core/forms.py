import json

from allauth.account.forms import LoginForm as BaseLoginForm
from allauth.account.forms import SignupForm as BaseSignupForm
from allauth.socialaccount.forms import SignupForm as BaseSocialSignupForm
from django import forms
from django.contrib.auth.password_validation import validate_password

from .models import PolicyDocument, User
from .runtime import SECRET_KEYS, get_setting, policy_languages
from .turnstile import verify_turnstile


class SignupForm(BaseSignupForm):
    policy_language = forms.ChoiceField(label="政策阅读语言 / Policy language")
    policy_versions = forms.CharField(widget=forms.HiddenInput)
    accept_policies = forms.BooleanField(
        label="我已阅读并同意隐私政策、使用条款和收录规则 / I accept the privacy policy, terms and inclusion rules"
    )

    def __init__(self, *args, **kwargs):
        from allauth.core import context

        self.request = kwargs.pop("request", None) or context.request
        super().__init__(*args, **kwargs)
        languages = policy_languages()
        self.fields["policy_language"].choices = [(language, language) for language in languages]
        if languages:
            self.fields["policy_versions"].initial = json.dumps(
                {
                    language: dict(
                        PolicyDocument.objects.filter(language=language).values_list("kind", "source_sha")
                    )
                    for language in languages
                }
            )

    def clean(self):
        data = super().clean()
        verify_turnstile(self.request)
        language = data.get("policy_language")
        current = dict(PolicyDocument.objects.filter(language=language).values_list("kind", "source_sha"))
        try:
            versions = json.loads(data.get("policy_versions", "{}"))
        except (ValueError, TypeError):
            versions = {}
        if set(current) != {"privacy", "terms", "inclusion"} or versions.get(language) != current:
            raise forms.ValidationError(
                "政策已更新，请刷新页面重新确认 / Policies have changed, please reload and confirm again"
            )
        data["policy_versions"] = current
        return data


class SocialSignupForm(BaseSocialSignupForm):
    password1 = forms.CharField(widget=forms.PasswordInput, label="密码 / Password")
    password2 = forms.CharField(widget=forms.PasswordInput, label="重复密码 / Password again")
    policy_language = forms.ChoiceField(label="政策阅读语言 / Policy language")
    policy_versions = forms.CharField(widget=forms.HiddenInput)
    accept_policies = forms.BooleanField(
        label="我同意隐私政策、使用条款和收录规则 / I accept the privacy policy, terms and inclusion rules"
    )

    def __init__(self, *args, **kwargs):
        from allauth.core import context

        self.request = kwargs.pop("request", None) or context.request
        super().__init__(*args, **kwargs)
        languages = policy_languages()
        self.fields["policy_language"].choices = [(language, language) for language in languages]
        self.fields["policy_versions"].initial = json.dumps(
            {
                language: dict(
                    PolicyDocument.objects.filter(language=language).values_list("kind", "source_sha")
                )
                for language in languages
            }
        )

    def clean(self):
        data = super().clean()
        verify_turnstile(self.request)
        if data.get("password1") != data.get("password2"):
            self.add_error("password2", "两次密码不一致 / Passwords do not match")
        if data.get("password1"):
            validate_password(data["password1"], self.sociallogin.user)
        language = data.get("policy_language")
        current = dict(PolicyDocument.objects.filter(language=language).values_list("kind", "source_sha"))
        try:
            versions = json.loads(data.get("policy_versions", "{}"))
        except (ValueError, TypeError):
            versions = {}
        if set(current) != {"privacy", "terms", "inclusion"} or versions.get(language) != current:
            raise forms.ValidationError("政策已更新，请刷新页面 / Policies have changed, reload this page")
        data["policy_versions"] = current
        return data


class LoginForm(BaseLoginForm):
    def clean(self):
        verify_turnstile(self.request)
        return super().clean()


class ProfileForm(forms.ModelForm):
    avatar = forms.ImageField(required=False, label="头像 / Avatar")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .avatar import avatar_available

        self.fields["avatar"].disabled = not avatar_available()
        if self.fields["avatar"].disabled:
            self.fields[
                "avatar"
            ].help_text = "头像存储未配置或不可用 / Avatar storage is not configured or unavailable"

    class Meta:
        model = User
        fields = ["username", "full_name", "public_email", "language"]
        labels = {
            "username": "用户名 / Username",
            "full_name": "姓名 / Name",
            "public_email": "公开邮箱 / Public email",
            "language": "界面语言 / UI language",
        }


class DeleteAccountForm(forms.Form):
    password = forms.CharField(widget=forms.PasswordInput, label="密码 / Password")
    confirm = forms.BooleanField(
        label="永久注销并撤下我的全部公开内容 / Permanently delete my account and all public entries"
    )

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_password(self):
        password = self.cleaned_data["password"]
        if not self.user.check_password(password):
            raise forms.ValidationError("密码不正确 / Incorrect password")
        return password


CONFIG_FIELDS = {
    "smtp_host": "SMTP host",
    "smtp_port": "SMTP port",
    "smtp_username": "SMTP username",
    "smtp_password": "SMTP password",
    "smtp_security": "SMTP security (starttls/tls)",
    "smtp_from_email": "From email",
    "operations_emails": "Operations emails (comma-separated)",
    "turnstile_site_key": "Turnstile site key",
    "turnstile_secret_key": "Turnstile secret key",
    "s3_endpoint": "S3 HTTPS endpoint",
    "s3_region": "S3 region",
    "s3_bucket": "S3 bucket",
    "s3_access_key": "S3 access key",
    "s3_secret_key": "S3 secret key",
    "s3_prefix": "S3 avatar prefix",
    "license_source_url": "License policy GitHub repository URL",
    "policy_source_url": "Policy documents repository URL",
    **{
        f"oauth_{p}_{key}": f"{p} OAuth {key}"
        for p in ("github", "gitee", "gitlab")
        for key in ("client_id", "client_secret")
    },
}


class ConfigurationForm(forms.Form):
    restore_schedule = forms.ChoiceField(
        choices=[
            ("off", "关闭 / Off"),
            ("weekly", "每周 / Weekly"),
            ("monthly", "每月 / Monthly"),
            ("quarterly", "每季度 / Quarterly"),
        ]
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for key, label in CONFIG_FIELDS.items():
            self.fields[key] = forms.CharField(
                required=False,
                label=label,
                widget=forms.PasswordInput(render_value=False) if key in SECRET_KEYS else forms.TextInput,
            )
            if key not in SECRET_KEYS:
                self.fields[key].initial = get_setting(key)
            elif get_setting(key):
                self.fields[key].help_text = "已配置，留空保持不变 / Configured; leave blank to retain"
        self.fields["restore_schedule"].initial = get_setting("restore_schedule", "monthly")
        self.fields["smtp_security"].initial = get_setting("smtp_security") or "starttls"
        self.fields["smtp_port"].initial = get_setting("smtp_port") or "587"
        self.fields[
            "smtp_security"
        ].help_text = "starttls 或 tls；本地 DEBUG 验证可使用 none。 / starttls or tls; none is allowed only in local DEBUG tests."

    def clean(self):
        from .git import validate_git_url

        data = super().clean()
        for key in ("policy_source_url", "license_source_url"):
            if data.get(key):
                try:
                    data[key] = validate_git_url(data[key], github_only=key == "license_source_url")
                except ValueError as exc:
                    self.add_error(key, str(exc))
        data["smtp_security"] = data.get("smtp_security") or "starttls"
        from django.conf import settings

        if data["smtp_security"] not in ("starttls", "tls"):
            if not (
                settings.DEBUG
                and data["smtp_security"] == "none"
                and data.get("smtp_host") in {"localhost", "127.0.0.1", "mailpit"}
            ):
                self.add_error(
                    "smtp_security", "Use starttls or tls (local DEBUG Mailpit alone may use none)"
                )
        if data.get("smtp_port"):
            try:
                if not 1 <= int(data["smtp_port"]) <= 65535:
                    raise ValueError
            except ValueError:
                self.add_error("smtp_port", "Invalid TCP port")
        if data.get("s3_endpoint"):
            from urllib.parse import urlsplit

            endpoint = urlsplit(data["s3_endpoint"])
            if (
                endpoint.scheme != "https"
                or endpoint.username
                or not endpoint.hostname
                or endpoint.query
                or endpoint.fragment
            ):
                self.add_error("s3_endpoint", "Use an HTTPS S3 endpoint without credentials")
        if data.get("s3_prefix") and (
            ".." in data["s3_prefix"].split("/") or data["s3_prefix"].startswith("/")
        ):
            self.add_error("s3_prefix", "Use a relative object prefix")
        for address in filter(None, (data.get("operations_emails") or "").split(",")):
            try:
                forms.EmailField().clean(address.strip())
            except forms.ValidationError:
                self.add_error("operations_emails", "Invalid operations email")
        return data


class BootstrapAccountForm(forms.Form):
    username = forms.CharField(max_length=32)
    email = forms.EmailField()
    password = forms.CharField(widget=forms.PasswordInput)

    def clean_username(self):
        from .models import username_validator

        username_validator(self.cleaned_data["username"])
        return self.cleaned_data["username"]

    def clean_password(self):
        value = self.cleaned_data["password"]
        validate_password(value)
        return value
