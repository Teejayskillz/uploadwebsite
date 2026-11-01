"""
Create (or rotate) a server-to-server API key.

    python manage.py create_api_client --name nzdworld-blog --permissions import,status

The secret is printed **once** and only its SHA-256 is stored, so it cannot be
recovered later. Put the two printed values in the blog's `.env`:

    UPLOADER_API_KEY_ID=<key_id>
    UPLOADER_API_SECRET=<secret>
"""

from __future__ import annotations

import secrets

from django.core.management.base import BaseCommand, CommandError

from uploader.models import ApiClient
from uploader.services.api_auth import derive_key

KNOWN_SCOPES = {"import", "status", "premium", "admin"}


class Command(BaseCommand):
    help = "Create or rotate an API client key for the main blog."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True, help="Client name, e.g. nzdworld-blog")
        parser.add_argument(
            "--permissions",
            default="import,status",
            help="Comma-separated scopes: import, status, premium, admin (or *).",
        )
        parser.add_argument(
            "--key-id",
            default="",
            help="Reuse a specific key_id (defaults to a random one on creation).",
        )

    def handle(self, *args, **options):
        name = options["name"].strip()
        if not name:
            raise CommandError("--name is required.")

        permissions = [item.strip() for item in options["permissions"].split(",") if item.strip()]
        invalid = [scope for scope in permissions if scope not in KNOWN_SCOPES and scope != "*"]
        if invalid:
            raise CommandError(f"Unknown scope(s): {', '.join(invalid)}")

        secret = secrets.token_urlsafe(32)
        existing = ApiClient.objects.filter(name=name).first()
        key_id = options["key_id"] or (existing.key_id if existing else secrets.token_hex(8))

        client, created = ApiClient.objects.update_or_create(
            name=name,
            defaults={
                "key_id": key_id,
                "secret_hash": derive_key(secret),
                "permissions": permissions,
                "is_active": True,
            },
        )

        action = "created" if created else "rotated"
        self.stdout.write(self.style.SUCCESS(f"API client {action}: {client.name} ({client.key_id})"))
        self.stdout.write(f"  permissions: {', '.join(client.permissions)}")
        self.stdout.write("")
        self.stdout.write("  Put these in the blog's .env (the secret is shown only once):")
        self.stdout.write(f"  UPLOADER_API_KEY_ID={client.key_id}")
        self.stdout.write(f"  UPLOADER_API_SECRET={secret}")
