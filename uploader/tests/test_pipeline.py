"""End-to-end pipeline tests with the network and ffmpeg stubbed out."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from uploader.models import BrandRule, ImportJob, SiteSetting, Video
from uploader.services import pipeline, queue
from uploader.services.downloader import DownloadResult
from uploader.services.netguard import ValidatedURL
from uploader.tests.support import make_scratch_dir, remove_scratch_dir

MP4_HEAD = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048


class PipelineTests(TestCase):
    def setUp(self):
        self.root = make_scratch_dir()
        self.addCleanup(remove_scratch_dir, self.root)
        self.overrides = override_settings(
            VIDEO_ROOT=str(self.root / "videos"),
            TEMP_ROOT=str(self.root / "tmp"),
        )
        self.overrides.enable()
        self.addCleanup(self.overrides.disable)

        SiteSetting.objects.update_or_create(
            pk=1,
            defaults={
                "allowed_formats": ["mp4", "mkv", "webm"],
                "max_file_size_bytes": 10 * 1024 * 1024,
                "low_storage_reserve_bytes": 0,
                "max_concurrent_jobs": 1,
            },
        )
        BrandRule.objects.create(name="nkiri", kind=BrandRule.Kind.TOKEN, pattern="nkiri")

        patcher = mock.patch.object(
            pipeline,
            "validate_url",
            return_value=ValidatedURL(
                url="https://example.com/file.mkv",
                scheme="https",
                host="example.com",
                port=443,
                addresses=("93.184.216.34",),
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_fetch(self, body: bytes = MP4_HEAD, container: str = "mp4"):
        def _fetch(target, destination, **kwargs):
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            if kwargs.get("on_progress"):
                kwargs["on_progress"](len(body), len(body))
            return DownloadResult(
                path=destination,
                size_bytes=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
                container=container,
                content_type=f"video/{container}",
                final_url="https://example.com/Extraction-3-2026-nkiri.com.mkv",
                hops=0,
            )

        return _fetch

    def _run(self, *, source_url="https://example.com/Extraction-3-2026-nkiri.com.mkv",
             title="Extraction 3", mode=ImportJob.FilenameMode.AUTO, body=MP4_HEAD,
             container="mp4"):
        job = queue.enqueue_import(
            source_url=source_url, title_hint=title, filename_mode=mode
        )
        claimed = queue.claim_next_job()
        assert claimed.pk == job.pk
        with mock.patch.object(pipeline, "fetch_to_file", side_effect=self._fake_fetch(body, container)):
            pipeline.process_job(claimed)
        claimed.refresh_from_db()
        return claimed

    def test_happy_path_stores_a_branded_file(self):
        job = self._run()
        self.assertEqual(job.status, ImportJob.Status.COMPLETED)
        self.assertIsNotNone(job.video)

        video = job.video
        self.assertEqual(video.stored_filename, "extraction-3-2026 | NZDWorld.com.mp4")
        self.assertEqual(video.extension, "mp4")
        self.assertEqual(video.size_bytes, len(MP4_HEAD))
        self.assertEqual(video.sha256, hashlib.sha256(MP4_HEAD).hexdigest())
        self.assertEqual(video.status, Video.Status.READY)

        stored = self.root / "videos" / video.relative_path
        self.assertTrue(stored.exists())
        self.assertEqual(stored.read_bytes(), MP4_HEAD)
        # The partial file must not survive a successful import.
        self.assertFalse((self.root / "tmp" / f"{job.public_id}.part").exists())

    def test_relative_path_is_year_month_partitioned(self):
        job = self._run()
        self.assertRegex(job.video.relative_path, r"^\d{4}/\d{2}/")

    def test_partial_extension_is_taken_from_content_not_the_url(self):
        """A .mkv URL serving MP4 must be stored as .mp4."""
        job = self._run(container="mp4")
        self.assertTrue(job.video.stored_filename.endswith(".mp4"))

    def test_identical_content_is_rejected_as_duplicate(self):
        first = self._run()
        second = queue.enqueue_import(
            source_url="https://example.com/another-name.mkv", title_hint="Another"
        )
        queue.claim_next_job()
        with mock.patch.object(pipeline, "fetch_to_file", side_effect=self._fake_fetch()):
            with self.assertRaises(pipeline.PipelineError) as ctx:
                pipeline.process_job(second)
        self.assertEqual(ctx.exception.code, "duplicate_content")
        second.refresh_from_db()
        # The duplicate must not create a second Video row.
        self.assertEqual(Video.objects.count(), 1)
        self.assertEqual(first.video_id, Video.objects.first().pk)

    def test_manual_filename_mode_is_used_verbatim_after_cleaning(self):
        job = queue.enqueue_import(
            source_url="https://example.com/x.mkv",
            title_hint="Ignored",
            filename_mode=ImportJob.FilenameMode.MANUAL,
            requested_filename="My Custom Name 2025 nkiri.com.mkv",
        )
        queue.claim_next_job()
        with mock.patch.object(pipeline, "fetch_to_file", side_effect=self._fake_fetch()):
            pipeline.process_job(job)
        job.refresh_from_db()
        self.assertEqual(job.video.stored_filename, "my-custom-name-2025 | NZDWorld.com.mp4")

    def test_two_imports_do_not_overwrite_each_other(self):
        # Different bytes -> different sha -> both stored.
        first = self._run(body=MP4_HEAD)
        second = self._run(
            source_url="https://example.com/second.mkv",
            title="Extraction 3",
            body=MP4_HEAD + b"different",
        )
        self.assertNotEqual(first.video.relative_path, second.video.relative_path)
        self.assertFalse(first.video.stored_filename == second.video.stored_filename)
        self.assertEqual(Video.objects.count(), 2)

    def test_provider_jobs_are_refused_while_the_adapter_is_disabled(self):
        job = queue.enqueue_import(
            source_url="https://wildshare.net/abc/movie",
            url_type=ImportJob.UrlType.PROVIDER,
            provider="wildshare",
        )
        queue.claim_next_job()
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.process_job(job)
        self.assertEqual(ctx.exception.code, "provider_not_enabled")

    def test_low_storage_is_refused_before_downloading(self):
        with mock.patch.object(pipeline, "free_bytes", return_value=10):
            with mock.patch.object(pipeline, "fetch_to_file") as fetch:
                job = queue.enqueue_import(
                    source_url="https://example.com/low.mkv", title_hint="Low"
                )
                queue.claim_next_job()
                with self.assertRaises(pipeline.PipelineError) as ctx:
                    pipeline.process_job(job)
                self.assertEqual(ctx.exception.code, "storage_low")
                fetch.assert_not_called()

    def test_unsafe_url_is_refused(self):
        with mock.patch.object(
            pipeline, "validate_url", side_effect=__import__(
                "uploader.services.netguard", fromlist=["UnsafeURLError"]
            ).UnsafeURLError("blocked")
        ):
            job = queue.enqueue_import(source_url="https://example.com/unsafe.mkv")
            queue.claim_next_job()
            with self.assertRaises(pipeline.PipelineError) as ctx:
                pipeline.process_job(job)
            self.assertEqual(ctx.exception.code, "unsafe_url")

    def test_storage_report_counts_stored_bytes(self):
        self._run()
        report = pipeline.storage_report()
        self.assertEqual(report["used_bytes"], len(MP4_HEAD))
        self.assertGreaterEqual(report["budget_bytes"], 0)
