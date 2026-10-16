"""A small, validated site logo stored with the database, independent of avatar S3."""
import base64
import hashlib
import warnings
from io import BytesIO

from django import forms
from django.db import transaction
from django.http import Http404, HttpResponse
from django.urls import reverse
from django.views.decorators.http import require_safe
from PIL import Image, ImageOps

from .i18n import text
from .runtime import get_setting, set_setting


def logo_url(request=None):
    revision = get_setting("site_logo_revision")
    return reverse("core:site-logo", args=[revision]) if revision else ""


class LogoForm(forms.Form):
    callback_url = provider_link = ""

    def __init__(self, *args, admin_style=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["logo"] = forms.FileField(
            label=text("选择新的 Logo", "Choose a new logo"),
            help_text=text("支持 PNG、JPEG、WebP，最大 5 MiB；建议使用透明背景。应用后统一显示于浏览器标签页、站点、后台和邮件，留空不会删除当前 Logo。",
                           "PNG, JPEG or WebP, up to 5 MiB. A transparent background is recommended. Applies to browser tabs, the site, admin and emails; leaving this blank retains the current logo."),
            widget=forms.FileInput(attrs={"accept": "image/png,image/jpeg,image/webp"}),
        )
        if admin_style:
            from unfold.widgets import UnfoldAdminFileFieldWidget
            self.fields["logo"].widget = UnfoldAdminFileFieldWidget(attrs={"accept": "image/png,image/jpeg,image/webp"})

    def clean_logo(self):
        upload = self.cleaned_data["logo"]
        if upload.size > 5 * 1024 * 1024:
            raise forms.ValidationError(text("图片不能超过 5 MiB。", "The image must not exceed 5 MiB."))
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(upload) as source:
                    if source.format not in {"PNG", "JPEG", "WEBP"} or source.width * source.height > 20_000_000:
                        raise ValueError
                    image = ImageOps.exif_transpose(source).convert("RGBA")
                    image.thumbnail((512, 512), Image.Resampling.LANCZOS)
                    output = BytesIO()
                    image.save(output, "PNG")
            return output.getvalue()
        except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise forms.ValidationError(text("请选择有效的 PNG、JPEG 或 WebP 图片，尺寸不超过 2000 万像素。", "Choose a valid PNG, JPEG or WebP image, no larger than 20 megapixels.")) from None


@transaction.atomic
def save_logo(data, user=None):
    set_setting("site_logo_data", base64.b64encode(data).decode("ascii"), user=user)
    set_setting("site_logo_revision", hashlib.sha256(data).hexdigest(), user=user)


@require_safe
def serve_logo(request, revision):
    encoded = get_setting("site_logo_data")
    if not encoded:
        raise Http404
    data = base64.b64decode(encoded)
    if hashlib.sha256(data).hexdigest() != revision:
        raise Http404
    response = HttpResponse(data, content_type="image/png")
    response["Cache-Control"] = "public, max-age=86400, immutable"
    response["X-Content-Type-Options"] = "nosniff"
    return response
