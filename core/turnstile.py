from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.core.exceptions import ValidationError

from .runtime import get_setting


def verify_turnstile(request):
    secret = get_setting("turnstile_secret_key")
    token = request.POST.get("cf-turnstile-response", "")
    if not secret or not token:
        raise ValidationError("请完成安全验证 / Please complete the security challenge")
    try:
        with httpx.Client(timeout=10) as client:
            result = client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": secret, "response": token},
            ).json()
        expected_host = urlsplit(settings.SITE_BASE_URL).hostname
        if not result.get("success") or result.get("hostname") != expected_host:
            raise ValidationError("安全验证未通过 / Security challenge failed")
    except (httpx.HTTPError, ValueError) as exc:
        raise ValidationError(
            "安全验证暂时不可用，请稍后重试 / Security verification is temporarily unavailable"
        ) from exc
