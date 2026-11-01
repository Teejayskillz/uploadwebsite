"""Tests for the deployment helper commands."""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from uploader.models import ApiClient, BrandRule, SiteSetting
from uploader.services.api_auth import derive_key


class BootstrapTests(TestCase):
    def test_bootstrap_creates_settings_and_rules(self):
        out = StringIO()
        call_command("bootstrap_uploader", stdout=out)
        setting = SiteSetting.load()
        self.assertIn("mp4", setting.allowed_formats)
        self.assertGreater(setting.max_file_size_bytes, 0)
        self.assertGreater(BrandRule.objects.count(), 0)
        self.assertIn("bootstrap_uploader finished", out.getvalue())

    def test_bootstrap_is_idempotent(self):
        call_command("bootstrap_uploader", stdout=StringIO())
        rules_after_first = BrandRule.objects.count()
        call_command("bootstrap_uploader", stdout=StringIO())
        self.assertEqual(BrandRule.objects.count(), rules_after_first)

    def test_bootstrap_respects_concurrency_option(self):
        call_command("bootstrap_uploader", "--concurrency", "2", stdout=StringIO())
        self.assertEqual(SiteSetting.load().max_concurrent_jobs, 2)


class CreateApiClientTests(TestCase):
    def test_creates_a_client_and_prints_the_secret_once(self):
        out = StringIO()
        call_command("create_api_client", "--name", "nzdworld-blog", stdout=out)
        printed = out.getvalue()

        client = ApiClient.objects.get(name="nzdworld-blog")
        self.assertTrue(client.is_active)
        self.assertEqual(client.permissions, ["import", "status"])
        self.assertIn(client.key_id, printed)
        self.assertIn("UPLOADER_API_SECRET=", printed)

        secret = printed.split("UPLOADER_API_SECRET=")[1].split()[0]
        self.assertEqual(client.secret_hash, derive_key(secret))
        # The stored value must never be the secret itself.
        self.assertNotEqual(client.secret_hash, secret)

    def test_rotation_changes_the_secret_but_keeps_the_key_id(self):
        first = StringIO()
        call_command("create_api_client", "--name", "blog", stdout=first)
        key_id = ApiClient.objects.get(name="blog").key_id
        first_hash = ApiClient.objects.get(name="blog").secret_hash

        second = StringIO()
        call_command("create_api_client", "--name", "blog", stdout=second)
        client = ApiClient.objects.get(name="blog")
        self.assertEqual(client.key_id, key_id)
        self.assertNotEqual(client.secret_hash, first_hash)
        self.assertIn("rotated", second.getvalue())

    def test_custom_permissions_and_rejection_of_unknown_scopes(self):
        call_command(
            "create_api_client", "--name", "ops", "--permissions", "import,premium", stdout=StringIO()
        )
        self.assertEqual(ApiClient.objects.get(name="ops").permissions, ["import", "premium"])

        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("create_api_client", "--name", "bad", "--permissions", "nonsense", stdout=StringIO())
