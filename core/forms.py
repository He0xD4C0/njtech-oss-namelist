import json

from allauth.account.forms import LoginForm as BaseLoginForm
from allauth.account.forms import ResetPasswordForm as BaseResetPasswordForm
from allauth.account.forms import SignupForm as BaseSignupForm
from allauth.socialaccount.forms import SignupForm as BaseSocialSignupForm
from django import forms
from django.contrib.auth.password_validation import validate_password

from .i18n import text
from .models import PolicyDocument, User
from .runtime import SECRET_KEYS, get_setting, policy_languages
from .turnstile import verify_turnstile


class VerifiedSignupMixin:
    proof_purpose = "signup"

    def clean_email(self):
        from allauth.account.models import EmailAddress

        from .email_verification import proven

        email = self.cleaned_data["email"].lower()
        if not proven(self.request, email, self.proof_purpose):
            raise forms.ValidationError(text("请先通过邮件链接验证此邮箱。", "Verify this email using the link first."))
        if EmailAddress.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(text("此邮箱已绑定其他账户。", "This email is already bound to another account."))
        return email

    def save(self, request):
        from .email_verification import registration_proof

        with registration_proof(request, self.cleaned_data["email"], self.proof_purpose):
            versions = dict(PolicyDocument.objects.select_for_update().filter(
                language=self.cleaned_data["policy_language"]
            ).values_list("kind", "source_sha"))
            if versions != self.cleaned_data["policy_versions"]:
                raise forms.ValidationError(text("政策已更新，请刷新页面重新确认。", "Policies have changed. Reload and confirm again."))
            return super().save(request)


class SignupForm(VerifiedSignupMixin, BaseSignupForm):
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


class SocialSignupForm(VerifiedSignupMixin, BaseSocialSignupForm):
    proof_purpose = "social"
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
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["login"].label = text("用户名或邮箱", "Username or email")
        self.fields["login"].widget.attrs.update(autocomplete="username", placeholder=self.fields["login"].label)

    def clean(self):
        verify_turnstile(self.request)
        return super().clean()


class ResetPasswordForm(BaseResetPasswordForm):
    def clean_email(self):
        from allauth.account.models import EmailAddress

        email = self.cleaned_data["email"].lower()
        self.users = [a.user for a in EmailAddress.objects.filter(
            email__iexact=email, verified=True, user__is_active=True
        ).select_related("user")]
        return email


class ProfileForm(forms.ModelForm):
    avatar = forms.ImageField(required=False, label="头像 / Avatar")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].disabled = True
        self.fields["username"].help_text = text("用户名是固定的账户标识，不可修改。", "Your username is a permanent account identifier.")
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
            "public_email": "公开默认邮箱 / Publish default email",
            "language": "界面语言 / UI language",
        }

    def save(self, commit=True):
        instance = super().save(commit=False)
        if commit:
            instance.save(update_fields=["full_name", "public_email", "language"])
        return instance


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


PLATFORM_NAMES = {"github": "GitHub", "gitee": "Gitee", "gitlab": "GitLab"}
PROVIDER_SETTINGS_URLS = {"github": "https://github.com/settings/developers", "gitee": "https://gitee.com/oauth/applications", "gitlab": "https://gitlab.com/-/user_settings/applications"}


CONFIG_FIELDS = {
    "operator_name": ("运营主体名称", "Operator name"),
    "operator_contact_email": ("公开联系邮箱", "Public contact email"),
    "smtp_host": ("邮件服务器地址", "SMTP host"),
    "smtp_port": ("端口", "SMTP port"),
    "smtp_security": ("连接加密方式", "Connection encryption"),
    "smtp_username": ("邮箱账号", "SMTP username"),
    "smtp_password": ("密码或客户端授权码", "Password or app password"),
    "smtp_from_email": ("发件邮箱", "From email"),
    "operations_emails": ("运维通知邮箱", "Operations notification emails"),
    "turnstile_site_key": ("站点公钥（Site key）", "Turnstile site key"),
    "turnstile_secret_key": ("验证密钥（Secret key）", "Turnstile secret key"),
    "s3_endpoint": ("存储服务地址（Endpoint）", "Storage endpoint"),
    "s3_bucket": ("存储桶名称（Bucket）", "Bucket name"),
    "s3_region": ("区域（Region）", "Region"),
    "s3_access_key": ("访问密钥 ID", "Access key ID"),
    "s3_secret_key": ("访问密钥", "Secret access key"),
    "s3_prefix": ("头像目录前缀", "Avatar object prefix"),
    "license_source_url": ("许可证策略仓库", "License policy repository"),
    "license_write_token": ("策略仓库写入令牌", "Policy repository write token"),
    "policy_source_url": ("政策正文仓库", "Policy documents repository"),
    **{f"oauth_{p}_{key}": (f"{PLATFORM_NAMES[p]} 应用{label}", f"{PLATFORM_NAMES[p]} {key.replace('_', ' ')}")
       for p in ("github", "gitee", "gitlab")
       for key, label in (("client_id", " ID"), ("client_secret", "密钥"))},
}

CONFIG_GROUPS = {
    "smtp": ("邮件服务 / Mail delivery", tuple(key for key in CONFIG_FIELDS if key.startswith("smtp_"))),
    "turnstile": ("安全验证 / Security verification", ("turnstile_site_key", "turnstile_secret_key")),
    "documents": ("政策正文 / Policy documents", ("policy_source_url", "operator_name", "operator_contact_email")),
    "licenses": ("许可证策略 / License policy", ("license_source_url", "license_write_token")),
    **{platform: (f"{PLATFORM_NAMES[platform]} 账号绑定 / {PLATFORM_NAMES[platform]} account linking",
                  (f"oauth_{platform}_client_id", f"oauth_{platform}_client_secret"))
       for platform in ("github", "gitee", "gitlab")},
    "storage": ("头像存储 / Avatar storage", tuple(key for key in CONFIG_FIELDS if key.startswith("s3_"))),
    "branding": ("站点 Logo / Site logo", ()),
    "backup": ("数据库备份 / Database backups", ()),
    "operations": ("运维选项 / Operations", ("operations_emails", "restore_schedule")),
}


def group_title(group):
    return text(*CONFIG_GROUPS[group][0].split(" / "))


REQUIRED_CONFIG_FIELDS = {
    "smtp": ("smtp_host", "smtp_port", "smtp_from_email"),
    "turnstile": ("turnstile_site_key", "turnstile_secret_key"),
    "documents": ("policy_source_url",), "licenses": ("license_source_url",),
    "storage": ("s3_bucket", "s3_access_key", "s3_secret_key"),
    **{p: (f"oauth_{p}_client_id", f"oauth_{p}_client_secret") for p in ("github", "gitee", "gitlab")},
}


class ConfigurationForm(forms.Form):
    def __init__(self, *args, group=None, admin_style=False, complete=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.group, self.complete = group, complete
        selected = CONFIG_GROUPS[group][1] if group else (*CONFIG_FIELDS, "restore_schedule")
        for key in selected:
            if key == "restore_schedule":
                self.fields[key] = forms.ChoiceField(
                    label=text("恢复演练频率", "Restore drill frequency"),
                    choices=[("off", text("关闭", "Off")), ("weekly", text("每周", "Weekly")),
                             ("monthly", text("每月", "Monthly")), ("quarterly", text("每季度", "Quarterly"))],
                    initial=get_setting(key) or "monthly",
                    help_text=text("设置多久验证一次备份能否恢复；需要服务器的独立备份配置先就绪。", "How often to verify that backups can be restored. Requires server-side backup configuration."),
                )
                continue
            field_class = forms.EmailField if key in {"operator_contact_email", "smtp_from_email"} else forms.CharField
            self.fields[key] = field_class(required=False, label=text(*CONFIG_FIELDS[key]),
                widget=forms.PasswordInput(render_value=False) if key in SECRET_KEYS else forms.TextInput)
            if key not in SECRET_KEYS:
                self.fields[key].initial = get_setting(key)
            else:
                self.fields[key].widget.attrs["autocomplete"] = "new-password"
        if "smtp_security" in self.fields:
            choices = [("tls", text("直接加密（SSL/TLS）", "Direct encryption (SSL/TLS)")),
                       ("starttls", text("连接后升级加密（STARTTLS）", "Upgrade encryption (STARTTLS)"))]
            from django.conf import settings
            if settings.DEBUG:
                choices.append(("none", text("仅本机测试：不加密", "Local testing only: unencrypted")))
            self.fields["smtp_security"] = forms.ChoiceField(
                required=False, label=text(*CONFIG_FIELDS["smtp_security"]), choices=choices,
                widget=forms.RadioSelect(attrs={"class": "form-check-input"}), initial=get_setting("smtp_security") or "starttls",
            )
        help_texts = {
            "license_write_token": ("仅允许选择上方这一个策略仓库的 GitHub 细粒度令牌（github_pat_），Contents 权限设为 Read and write。加密保存，仅后台服务读取；管理员审批会直接提交默认分支。", "Fine-grained token scoped only to the policy repository, with Contents read/write. Stored encrypted; approvals commit to its default branch."),
            "smtp_host": ("填写邮箱服务商提供的 SMTP 地址，不需要 https://。", "Enter the SMTP hostname from your email provider, without https://."),
            "smtp_port": ("直接加密常用 465，STARTTLS 常用 587；服务商给出的 994 等端口也可使用。切换加密方式不会改动端口。", "Common ports: 465 for SSL/TLS, 587 for STARTTLS. Provider-specific ports such as 994 are supported and never overwritten."),
            "smtp_security": ("两种方式都加密传输，请与邮箱服务商的要求保持一致。", "Both options encrypt the connection. Match your provider’s instructions."),
            "smtp_username": ("通常是完整邮箱地址；仅在服务商不要求认证时留空。", "Usually your full email address. Leave blank only if your provider does not require authentication."),
            "smtp_password": ("网易等服务商通常要求客户端授权码，而不是网页登录密码。", "Some providers require an app password rather than your webmail password."),
            "smtp_from_email": ("收件人看到的发件地址；通常须与邮箱账号一致或已获服务商授权。", "The sender shown to recipients. Usually this must match your account or be an authorized sender."),
            "operator_name": ("用于政策正文；留空时显示“本站运营者”。", "Used in policy documents. Defaults to “Site operator”."),
            "operator_contact_email": ("会公开显示在政策中；不是 SMTP 账号或测试收件邮箱。", "Publicly displayed in policies; separate from SMTP and test-recipient addresses."),
            "policy_source_url": ("粘贴公开仓库的 HTTPS 地址，带不带 .git 都可以，系统自动处理。", "Paste a public repository HTTPS URL, with or without .git; both are accepted."),
            "license_source_url": ("粘贴公开 GitHub 仓库的 HTTPS 地址，带不带 .git 都可以。读取 whitelist/ 和 blacklist/ 中的许可证正文。", "Paste a public GitHub repository HTTPS URL, with or without .git. License texts are read from whitelist/ and blacklist/."),
            "s3_endpoint": ("填写 S3 API 服务地址。R2 示例：https://&lt;账户ID&gt;.r2.cloudflarestorage.com，不要在末尾加桶名。其他 S3 网关可保留服务商要求的路径。", "Use the S3 API endpoint. R2: https://&lt;ACCOUNT_ID&gt;.r2.cloudflarestorage.com, without the bucket name. Other S3 gateways may require a path."),
            "s3_bucket": ("只填桶名，例如 njtech-oss-namelist，不是网址。", "Only the bucket name, e.g. njtech-oss-namelist, not a URL."),
            "s3_region": ("Cloudflare R2 填 auto；其他服务按服务商说明填写。", "For Cloudflare R2 enter auto; otherwise follow your provider’s instructions."),
            "s3_prefix": ("桶内存放头像的相对目录，例如 avatars；不是桶名，也不是网址。", "A relative directory inside the bucket, e.g. avatars; not the bucket name or a URL."),
            "s3_access_key": ("使用允许访问这个桶的 S3 API 密钥 ID。", "Use an S3 API access key ID authorized for this bucket."),
            "s3_secret_key": ("与访问密钥 ID 配套的 Secret Access Key。", "The secret access key paired with the access key ID."),
            "operations_emails": ("接收同步失败等运维通知；多个邮箱用英文逗号分隔，可留空。", "Receives operational alerts such as failed syncs. Separate addresses with commas; optional."),
            "turnstile_site_key": ("在 Cloudflare Turnstile 创建站点后获取，并允许实际访问本站的主机名。", "Create a site in Cloudflare Turnstile and allow this site’s actual hostname."),
            "turnstile_secret_key": ("与上面的站点公钥配套；仅服务器使用。", "Paired with the site key above; used only by the server."),
        }
        for key, field in self.fields.items():
            if key in help_texts:
                field.help_text = text(*help_texts[key])
            if key in SECRET_KEYS and get_setting(key):
                field.help_text = text("已配置，留空保留。", "Configured; leave blank to retain. ") + " " + (field.help_text or "")
        for key, default in {"smtp_port": "587", "s3_prefix": "avatars"}.items():
            if key in self.fields:
                self.fields[key].initial = get_setting(key) or default
        if "smtp_port" in self.fields:
            self.fields["smtp_port"].widget = forms.NumberInput(attrs={"min": 1, "max": 65535})
        if complete:
            for key in REQUIRED_CONFIG_FIELDS.get(group, ()):
                self.fields[key].required = not (key in SECRET_KEYS and get_setting(key))
        self.callback_url = self.provider_link = ""
        if group in PLATFORM_NAMES:
            from django.conf import settings
            self.provider_link = PROVIDER_SETTINGS_URLS[group]
            self.callback_url = settings.SITE_BASE_URL.rstrip("/") + f"/accounts/{group}/login/callback/"
            self.fields[f"oauth_{group}_client_id"].help_text = text("创建 OAuth 应用后复制应用 ID，不是个人访问令牌。", "Copy the client ID from your OAuth application, not a personal access token.")
            secret_field = self.fields[f"oauth_{group}_client_secret"]
            secret_field.help_text = (secret_field.help_text or "") + text(" 填写同一个 OAuth 应用的客户端密钥。", " Use the client secret from the same OAuth application.")
        if admin_style:
            from unfold.widgets import INPUT_CLASSES, SELECT_CLASSES, UnfoldAdminRadioSelectWidget
            for field in self.fields.values():
                if isinstance(field.widget, forms.RadioSelect):
                    field.widget = UnfoldAdminRadioSelectWidget(choices=field.choices)
                else:
                    field.widget.attrs["class"] = " ".join(SELECT_CLASSES if isinstance(field.widget, forms.Select) else INPUT_CLASSES)

    def clean(self):
        from urllib.parse import urlsplit

        from django.conf import settings

        from .git import validate_git_url

        data = super().clean()
        if data.get("license_write_token") and not data["license_write_token"].startswith("github_pat_"):
            self.add_error("license_write_token", "请使用仅授权当前策略仓库的 GitHub 细粒度令牌（github_pat_），不要使用经典令牌或 OAuth 令牌。")
        for key in ("policy_source_url", "license_source_url"):
            if data.get(key):
                try:
                    data[key] = validate_git_url(data[key], github_only=key == "license_source_url")
                except ValueError:
                    self.add_error(key, text("请填写受支持平台的公开仓库 HTTPS 地址。", "Enter a supported public repository HTTPS URL."))
        if "smtp_security" in self.fields:
            data["smtp_security"] = data.get("smtp_security") or "starttls"
            if data["smtp_security"] == "none" and not (settings.DEBUG and data.get("smtp_host") in {"localhost", "127.0.0.1", "mailpit"}):
                self.add_error("smtp_security", text("请为邮件服务选择一种加密方式。", "Select an encrypted connection for your mail service."))
        if data.get("smtp_port"):
            try:
                if not 1 <= int(data["smtp_port"]) <= 65535:
                    raise ValueError
            except ValueError:
                self.add_error("smtp_port", text("端口须为 1–65535 之间的整数。", "Enter a port number between 1 and 65535."))
        if data.get("smtp_host") and any(c in data["smtp_host"] for c in "/:@ "):
            self.add_error("smtp_host", text("只填写服务器主机名，不要包含协议或端口。", "Enter only the server hostname, without protocol or port."))
        if data.get("s3_endpoint"):
            try:
                endpoint = urlsplit(data["s3_endpoint"])
            except ValueError:
                endpoint = urlsplit("")
            if endpoint.scheme != "https" or endpoint.username or not endpoint.hostname or endpoint.query or endpoint.fragment:
                self.add_error("s3_endpoint", text("请填写 HTTPS 服务地址，不要包含账号、查询参数或片段。", "Use an HTTPS endpoint without credentials, query parameters or fragments."))
            elif endpoint.hostname.endswith(".r2.cloudflarestorage.com"):
                if endpoint.path.strip("/"):
                    self.add_error("s3_endpoint", text("R2 服务地址不能带桶名路径；请把桶名单独填入“存储桶名称”。", "Remove the bucket path from the R2 endpoint and enter it in Bucket name."))
                if not data.get("s3_region"):
                    data["s3_region"] = "auto"
        if data.get("s3_bucket") and any(c in data["s3_bucket"] for c in "/:"):
            self.add_error("s3_bucket", text("只填写桶名，不要填写网址或目录。", "Enter only the bucket name, not a URL or directory."))
        if data.get("s3_prefix") and (".." in data["s3_prefix"].split("/") or data["s3_prefix"].startswith("/")):
            self.add_error("s3_prefix", text("请填写桶内的相对目录，例如 avatars。", "Use a relative object prefix, e.g. avatars."))
        for address in filter(None, (data.get("operations_emails") or "").split(",")):
            try:
                forms.EmailField().clean(address.strip())
            except forms.ValidationError:
                self.add_error("operations_emails", text("请检查通知邮箱，多个邮箱用英文逗号分隔。", "Check the email addresses and separate them with commas."))

        if self.complete:
            for key in REQUIRED_CONFIG_FIELDS.get(self.group, ()):
                if key not in self.errors and not data.get(key) and not (key in SECRET_KEYS and get_setting(key)):
                    self.add_error(key, text("请填写此项后继续。", "Complete this field to continue."))
            if self.group == "smtp" and data.get("smtp_username") and not (data.get("smtp_password") or get_setting("smtp_password")):
                self.add_error("smtp_password", text("已填写邮箱账号，请同时提供密码或授权码。", "Provide a password or app password for this account."))
        return data


class SMTPTestForm(forms.Form):
    recipient = forms.EmailField(required=False)

    def __init__(self, *args, admin_style=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["recipient"].label = text("测试收件邮箱", "Test recipient")
        self.fields["recipient"].initial = get_setting("smtp_from_email")
        self.fields["recipient"].help_text = text("默认发给发件邮箱自身，也可改为你能查看的邮箱；不需要先创建管理员。", "Defaults to the sender itself. You can use any inbox you can check; no administrator account is needed.")
        if admin_style:
            from unfold.widgets import INPUT_CLASSES

            self.fields["recipient"].widget.attrs["class"] = " ".join(INPUT_CLASSES)


class BootstrapAccountForm(forms.Form):
    username = forms.CharField(max_length=32, initial="admin", disabled=True, label="用户名 / Username")
    email = forms.EmailField(required=False, label="管理员邮箱 / Administrator email")
    password = forms.CharField(widget=forms.PasswordInput, label="密码 / Password")

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)
        for key, labels in {"username": ("用户名", "Username"), "email": ("管理员邮箱", "Administrator email"), "password": ("登录密码", "Login password")}.items():
            self.fields[key].label = text(*labels)
        self.fields["username"].help_text = text("唯一超管固定为 admin，角色不可转让。", "The only superuser is admin. This role cannot be transferred.")
        self.fields["email"].help_text = text("可留空；填写后须先通过邮件链接验证。", "Optional. Verify the address using the email link before creating the account.")
        self.fields["password"].widget.attrs["autocomplete"] = "new-password"
        self.fields["password"].help_text = text("请设置独立的站点登录密码，不要填写邮箱授权码。", "Choose a separate site password, not your email app password.")

    def clean_username(self):
        from .models import username_validator

        username_validator(self.cleaned_data["username"])
        if User.objects.filter(username=self.cleaned_data["username"]).exists():
            raise forms.ValidationError(text("用户名已存在", "Username already exists"))
        return self.cleaned_data["username"]

    def clean_email(self):
        from allauth.account.models import EmailAddress

        from .email_verification import proven

        email = self.cleaned_data["email"].lower()
        if email and (not self.request or not proven(self.request, email, "setup")):
            raise forms.ValidationError(text("请先验证邮箱，或留空跳过。", "Verify the email first, or leave it blank."))
        if email and EmailAddress.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(text("邮箱已存在", "Email already exists"))
        return email

    def clean_password(self):
        value = self.cleaned_data["password"]
        validate_password(value)
        return value
