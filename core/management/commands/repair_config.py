import getpass
import sys

from django.core.management.base import BaseCommand, CommandError

from core.runtime import SECRET_KEYS, SETTING_KEYS, set_setting


class Command(BaseCommand):
    help = "Server-only encrypted runtime configuration repair; secrets are never printed."

    def add_arguments(self, parser):
        parser.add_argument("key", choices=sorted(SETTING_KEYS))
        parser.add_argument("--value")
        parser.add_argument("--stdin", action="store_true")
        parser.add_argument("--clear", action="store_true")

    def handle(self, *args, **options):
        key = options["key"]
        if key in SECRET_KEYS and options["value"] is not None:
            raise CommandError("Do not pass secrets in shell arguments; use --stdin or the hidden prompt")
        value = (
            ""
            if options["clear"]
            else (sys.stdin.readline().rstrip("\r\n") if options["stdin"] else options["value"])
        )
        if value is None:
            value = getpass.getpass(f"New value for {key}: ")
        set_setting(key, value)
        self.stdout.write(self.style.SUCCESS(f"Updated {key}; no value displayed"))
