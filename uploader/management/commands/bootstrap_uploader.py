"""
Seed the rows a fresh deployment needs before the first import.

    python manage.py bootstrap_uploader

Idempotent: running it twice changes nothing. It creates the singleton
``SiteSetting`` with production-safe defaults and a starter set of brand-removal
rules for the source sites currently seen in NZDWorld filenames.

Deliberately conservative: only site/brand tokens are added (never title words),
and each rule matches a whole separator-delimited token, so a movie called
"Nkirika" is untouched by the "nkiri" rule.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from uploader.models import BrandRule, SiteSetting

DEFAULT_FORMATS = ["mp4", "mkv", "webm", "mov", "m4v"]

# Known source-site brands in this catalogue. Extend from the admin.
DEFAULT_BRAND_TOKENS = [
    ("nkiri", "nkiri"),
    ("thenkiri", "thenkiri"),
    ("downloadwella", "downloadwella"),
    ("naijarocks", "naijarocks"),
    ("hypeblog9ja", "hypeblog9ja"),
    ("9jarocks", "9jarocks"),
    ("nzdworld", "nzdworld"),
    ("jaraflix", "jaraflix"),
    ("naijaloaded", "naijaloaded"),
    ("fzmovies", "fzmovies"),
    ("netnaija", "netnaija"),
]


class Command(BaseCommand):
    help = "Create the singleton settings row and default brand rules."

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-file-gb",
            type=float,
            default=4.0,
            help="Maximum individual video size in GB (default 4).",
        )
        parser.add_argument(
            "--concurrency",
            type=int,
            default=1,
            help="Maximum simultaneous imports (default 1 on shared hosting).",
        )

    def handle(self, *args, **options):
        setting = SiteSetting.load()
        changed = False

        if not setting.allowed_formats:
            setting.allowed_formats = list(DEFAULT_FORMATS)
            changed = True
        if setting.max_file_size_bytes <= 0:
            setting.max_file_size_bytes = int(options["max_file_gb"] * 1024**3)
            changed = True
        if options["concurrency"] and setting.max_concurrent_jobs != options["concurrency"]:
            setting.max_concurrent_jobs = max(1, options["concurrency"])
            changed = True
        if changed:
            setting.save()
            self.stdout.write(
                f"SiteSetting updated: formats={setting.allowed_formats} "
                f"max={setting.max_file_size_bytes} bytes concurrency={setting.max_concurrent_jobs}"
            )
        else:
            self.stdout.write("SiteSetting already configured; left unchanged.")

        created_rules = 0
        for order, (name, pattern) in enumerate(DEFAULT_BRAND_TOKENS):
            _, created = BrandRule.objects.get_or_create(
                name=name,
                defaults={
                    "kind": BrandRule.Kind.TOKEN,
                    "pattern": pattern,
                    "is_active": True,
                    "display_order": order,
                },
            )
            created_rules += int(created)

        self.stdout.write(
            f"Brand rules: {BrandRule.objects.count()} total ({created_rules} created)."
        )
        self.stdout.write(self.style.SUCCESS("bootstrap_uploader finished."))
