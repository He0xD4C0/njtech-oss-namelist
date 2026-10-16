from urllib.parse import urlsplit

from django import forms

from deploy.backup import managed

from .backup import LOCATION, SECRETS, candidate
from .i18n import text
from .models import BackupConfiguration


class BackupConfigurationForm(forms.Form):
    callback_url = provider_link = ""

    def __init__(self, *args, admin_style=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.record = BackupConfiguration.objects.filter(pk=1).first()
        self.saved = candidate(self.record) if self.record else {}
        labels = {
            "endpoint": ("备份服务地址（Endpoint）", "Backup endpoint"),
            "bucket": ("备份存储桶", "Backup bucket"),
            "region": ("区域（Region）", "Region"),
            "access_key": ("访问密钥 ID", "Access key ID"),
            "secret_key": ("访问密钥", "Secret access key"),
        }
        for key, label in labels.items():
            self.fields[key] = forms.CharField(label=text(*label), required=not (key in SECRETS and self.saved.get(key)),
                initial=self.saved.get(key, "") if key not in SECRETS else None,
                widget=forms.PasswordInput(render_value=False) if key in SECRETS else forms.TextInput)
        self.fields["endpoint"].help_text = text(
            "填写 HTTPS 服务地址，例如 https://<账户ID>.r2.cloudflarestorage.com；不要附带桶名或目录。",
            "Enter the HTTPS service endpoint, e.g. https://<ACCOUNT_ID>.r2.cloudflarestorage.com, without a bucket or path.")
        if self.saved.get("endpoint"):
            self.fields["endpoint"].initial = "https://" + self.saved["endpoint"]
        self.fields["bucket"].help_text = text("数据库备份专用桶，不会自动使用头像存储。", "A bucket for database backups; avatar storage is not reused.")
        self.fields["region"].help_text = text("Cloudflare R2 填 auto，其他服务按服务商说明填写。", "Use auto for Cloudflare R2; otherwise follow your provider’s instructions.")
        self.fields["uri_style"] = forms.ChoiceField(label=text("高级：寻址方式", "Advanced: addressing style"),
            choices=[("path", text("路径式（兼容多数 S3 服务）", "Path style (compatible with most S3 services)")),
                     ("host", text("虚拟主机式", "Virtual host style"))], initial=self.saved.get("uri_style", "path"))
        for key in SECRETS:
            self.fields[key].widget.attrs["autocomplete"] = "new-password"
            self.fields[key].help_text = text("已配置，留空保留。", "Configured; leave blank to retain.") if self.saved.get(key) else ""
        if self.record and self.record.location:
            for key in LOCATION:
                self.fields[key].disabled = True
                self.fields[key].help_text = text("备份已启用，存储位置固定；可更新访问凭据。", "Backup location is fixed after activation; access credentials can be updated.")
        if not managed.enabled():
            for field in self.fields.values():
                field.disabled = True
        if admin_style:
            from unfold.widgets import INPUT_CLASSES, SELECT_CLASSES
            for field in self.fields.values():
                field.widget.attrs["class"] = " ".join(SELECT_CLASSES if isinstance(field.widget, forms.Select) else INPUT_CLASSES)

    def clean(self):
        data = super().clean()
        endpoint = data.get("endpoint", "")
        try:
            parsed = urlsplit(endpoint if "://" in endpoint else "https://" + endpoint)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.port not in {None, 443}):
                raise ValueError
            data["endpoint"] = parsed.hostname.lower()
        except ValueError:
            self.add_error("endpoint", text("填写 HTTPS 服务地址，桶名单独填写；不支持目录、账号、查询参数或自定义端口。", "Use an HTTPS service endpoint; enter the bucket separately. Paths, credentials, queries and custom ports are not supported."))
        for key, value in data.items():
            if any(c in value for c in "\r\n\x00"):
                self.add_error(key, text("不允许换行或控制字符。", "Line breaks and control characters are not allowed."))
        if data.get("bucket") and any(c in data["bucket"] for c in "/: "):
            self.add_error("bucket", text("只填写桶名，不要填写网址或目录。", "Enter only the bucket name, without a URL or path."))
        return data
