"""Model behaviour tests, including the duplicate-import guarantee."""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from uploader.models import (
    ApiClient,
    ImportJob,
    PremiumAssertion,
    SiteSetting,
    Video,
)


class SiteSettingTests(TestCase):
    def test_load_creates_and_reuses_the_singleton(self):
        first = SiteSetting.load()
        second = SiteSetting.load()
        self.assertEqual(first.pk, 1)
        self.assertEqual(second.pk, 1)
        self.assertEqual(SiteSetting.objects.count(), 1)

    def test_save_forces_primary_key_one(self):
        setting = SiteSetting(max_concurrent_jobs=3)
        setting.save()
        self.assertEqual(setting.pk, 1)


class VideoTests(TestCase):
    def _video(self, **kwargs):
        defaults = {
            "title": "Extraction 3",
            "original_filename": "Extraction-3-2026-nkiri.com.mkv",
            "stored_filename": "extraction-3-2026 | NZDWorld.com.mkv",
            "relative_path": f"2026/11/{uuid.uuid4().hex}.mkv",
            "extension": "mkv",
            "size_bytes": 1024,
        }
        defaults.update(kwargs)
        return Video.objects.create(**defaults)

    def test_public_id_is_an_opaque_32_char_hex(self):
        video = self._video()
        self.assertEqual(len(video.public_id), 32)
        int(video.public_id, 16)  # raises if not hex

    def test_public_ids_are_unique(self):
        self.assertNotEqual(self._video().public_id, self._video().public_id)

    def test_download_url_uses_public_id_not_a_path(self):
        video = self._video()
        url = video.download_url
        self.assertIn(video.public_id, url)
        self.assertNotIn(video.relative_path, url)

    def test_is_available_requires_ready_and_not_deleted(self):
        video = self._video()
        self.assertTrue(video.is_available)
        video.status = Video.Status.PROCESSING
        self.assertFalse(video.is_available)


class ImportJobDedupeTests(TestCase):
    """The database itself must refuse two active jobs for one source."""

    def _job(self, dedupe_key):
        return ImportJob.objects.create(
            source_url="https://example.com/movie.mp4",
            title_hint="Movie",
            dedupe_key=dedupe_key,
            active_dedupe_key=dedupe_key,
        )

    def test_second_active_job_for_same_source_is_rejected(self):
        self._job("abc123")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._job("abc123")

    def test_finished_jobs_release_the_dedupe_slot(self):
        first = self._job("abc123")
        first.mark_terminal(ImportJob.Status.COMPLETED)

        second = self._job("abc123")  # must not raise
        self.assertEqual(second.status, ImportJob.Status.QUEUED)
        self.assertEqual(ImportJob.objects.filter(dedupe_key="abc123").count(), 2)

    def test_mark_terminal_clears_active_key_and_sets_finish_time(self):
        job = self._job("abc123")
        job.mark_terminal(ImportJob.Status.FAILED, error_code="download_failed", error_message="boom")
        job.refresh_from_db()
        self.assertIsNone(job.active_dedupe_key)
        self.assertEqual(job.error_code, "download_failed")
        self.assertIsNotNone(job.finished_at)
        self.assertEqual(job.progress_percent, 0)

    def test_mark_terminal_completed_sets_progress_100(self):
        job = self._job("abc123")
        job.mark_terminal(ImportJob.Status.COMPLETED)
        job.refresh_from_db()
        self.assertEqual(job.progress_percent, 100)

    def test_null_active_keys_do_not_collide(self):
        job_a = self._job("aaa")
        job_b = self._job("bbb")
        job_a.mark_terminal(ImportJob.Status.CANCELLED)
        job_b.mark_terminal(ImportJob.Status.CANCELLED)
        # Both finished; many more NULLs must be acceptable.
        for _ in range(3):
            ImportJob.objects.create(source_url="https://example.com/x.mp4", dedupe_key="zzz")
        self.assertEqual(ImportJob.objects.filter(active_dedupe_key__isnull=True).count(), 5)

    def test_status_helpers(self):
        job = self._job("abc123")
        self.assertTrue(job.is_active is False)  # queued is neither active nor terminal
        self.assertFalse(job.is_terminal)
        job.status = ImportJob.Status.DOWNLOADING
        self.assertTrue(job.is_active)
        job.status = ImportJob.Status.COMPLETED
        self.assertTrue(job.is_terminal)


class AccessModelTests(TestCase):
    def test_api_client_permissions(self):
        client = ApiClient.objects.create(
            name="nzdworld-blog", key_id="abc", secret_hash="x", permissions=["import"]
        )
        self.assertTrue(client.allows("import"))
        self.assertFalse(client.allows("admin"))
        client.is_active = False
        self.assertFalse(client.allows("import"))

    def test_wildcard_permission(self):
        client = ApiClient.objects.create(
            name="ops", key_id="ops", secret_hash="x", permissions=["*"]
        )
        self.assertTrue(client.allows("anything"))

    def test_premium_assertion_expiry(self):
        now = timezone.now()
        expired = PremiumAssertion.objects.create(
            subject_id="42", is_premium=True, expires_at=now - timedelta(minutes=1)
        )
        live = PremiumAssertion.objects.create(
            subject_id="43", is_premium=True, expires_at=now + timedelta(minutes=5)
        )
        self.assertFalse(expired.is_current())
        self.assertTrue(live.is_current())

    def test_free_assertion_is_never_current(self):
        assertion = PremiumAssertion.objects.create(
            subject_id="44",
            is_premium=False,
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        self.assertFalse(assertion.is_current())
