import smtplib
import ssl
from urllib.parse import urlsplit

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.mail.backends.smtp import EmailBackend
from django.template.loader import render_to_string
from django.utils.translation import get_language

from .i18n import text
from .runtime import get_setting


def style_email(message, *, action_url="", action_label=""):
    """One HTML layout for all transactional mail; keep a plain-text alternative."""
    if urlsplit(action_url).scheme not in {"http", "https"}:
        action_url = ""
    from .branding import logo_url
    site_url = settings.SITE_BASE_URL.rstrip("/")
    logo = logo_url()
    body = message.body
    html = render_to_string("emails/message.html", {
        "title": message.subject, "body": body, "action_url": action_url,
        "action_label": action_label, "site_url": site_url,
        "language": get_language() or "zh-hans",
        "brand_logo_url": site_url + logo if logo else "",
    })
    if action_url and action_url not in body:
        message.body += "\n\n" + action_label + ": " + action_url
    message.body += "\n\nNJTech OSS\n" + site_url + "\n"
    message.attach_alternative(html, "text/html")
    return message


def send_system_mail(subject, body, recipients, *, action_url="", action_label=""):
    message = EmailMultiAlternatives(subject, body, get_setting("smtp_from_email") or None, recipients)
    return style_email(message, action_url=action_url, action_label=action_label).send()


def send_smtp_test(recipient):
    return send_system_mail(
        text("NJTech OSS · 邮件服务测试", "NJTech OSS · Mail delivery test"),
        text("这是一封由站点初始化或运行时配置发送的测试邮件。\n\n收到这封邮件表示该收件邮箱已收到本次测试投递。你可以返回站点继续配置。",
             "This test email was sent from site setup or runtime settings.\n\nReceiving it confirms delivery to this inbox. You can return to the site to continue."),
        [recipient],
    )


def mail_error(exc):
    # Never expose server replies: they can contain addresses, credentials or URLs.
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return text("邮箱认证失败：请检查账号和客户端授权码，并确认已启用 SMTP。", "Authentication failed. Check the account/app password and enable SMTP with your provider.")
    if isinstance(exc, (ssl.SSLError, smtplib.SMTPNotSupportedError)):
        return text("加密连接失败：请核对加密方式、端口与服务器证书。", "Secure connection failed. Check encryption mode, port and server certificate.")
    if isinstance(exc, (smtplib.SMTPSenderRefused, smtplib.SMTPRecipientsRefused)):
        return text("邮件地址被服务商拒绝：请检查发件权限和收件邮箱。", "The provider rejected an address. Check sender authorization and the recipient address.")
    if isinstance(exc, smtplib.SMTPServerDisconnected) or (isinstance(exc, OSError) and not isinstance(exc, smtplib.SMTPException)):
        return text("无法连接邮件服务器或连接超时：请检查服务器地址、端口与网络后重试。", "Connection failed or timed out. Check hostname, port and network, then retry.")
    return text("邮件服务器未接受本次邮件，请检查邮箱服务商的发送限制后重试。", "The mail server did not accept this message. Check your provider’s sending restrictions, then retry.")



class RuntimeEmailBackend(EmailBackend):
    def __init__(self, *args, **kwargs):
        security = get_setting("smtp_security") or "starttls"
        kwargs.update(
            host=get_setting("smtp_host"),
            port=int(get_setting("smtp_port") or "587"),
            username=get_setting("smtp_username"),
            password=get_setting("smtp_password"),
            use_tls=security == "starttls",
            use_ssl=security == "tls",
            timeout=15,
        )
        super().__init__(*args, **kwargs)

    def send_messages(self, email_messages):
        if not self.host:
            raise ConnectionError("SMTP has not been configured")
        for message in email_messages:
            message.from_email = get_setting("smtp_from_email") or message.from_email
        return super().send_messages(email_messages)
