from django.core.management.base import BaseCommand, CommandError

from core.crypto import decrypt
from core.models import RuntimeSetting


class Command(BaseCommand):
    help = "Read-only decryption proof for isolated restored DB; starts no business tasks."

    def handle(self, *args, **options):
        from providers.models import OAuthBinding

        count = 0
        try:
            for record in RuntimeSetting.objects.filter(secret=True):
                decrypt(record.value)
                count += 1
            for binding in OAuthBinding.objects.all():
                decrypt(binding.encrypted_access_token)
                decrypt(binding.encrypted_refresh_token)
                count += 1
        except Exception:
            raise CommandError(
                "Restored configuration cannot be decrypted with the supplied server key"
            ) from None
        self.stdout.write(f"Decryption verified for {count} encrypted records; no external actions performed")
