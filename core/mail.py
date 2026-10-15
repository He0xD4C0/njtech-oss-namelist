from django.core.mail.backends.smtp import EmailBackend

from .runtime import get_setting


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
