"""Import-queue behaviour tests (brief sections 7 and 18)."""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from uploader.models import ApiNonce, ImportJob, SiteSetting
from uploader.services import queue


def make_job(**kwargs) -> ImportJob:
    defaults = {
        "source_url": "https://example.com/movie.mp4",
        "title_hint": "Movie",
    }
    defaults.update(kwargs)
    return queue.enqueue_import(**defaults)


class DedupeKeyTests(TestCase):
    def test_equivalent_urls_share_a_key(self):
        a = queue.compute_dedupe_key("https://example.com/movie.mp4")
        b = queue.compute_dedupe_key("HTTPS://Example.COM/movie.mp4")
        c = queue.compute_dedupe_key("https://example.com:443/movie.mp4#frag")
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_different_urls_differ(self):
        self.assertNotEqual(
            queue.compute_dedupe_key("https://example.com/a.mp4"),
            queue.compute_dedupe_key("https://example.com/b.mp4"),
        )

    def test_query_string_is_significant(self):
        self.assertNotEqual(
            queue.compute_dedupe_key("https://example.com/a.mp4?token=1"),
            queue.compute_dedupe_key("https://example.com/a.mp4?token=2"),
        )

    def test_empty_url_is_rejected(self):
        with self.assertRaises(queue.QueueError):
            queue.compute_dedupe_key("")


class EnqueueTests(TestCase):
    def test_enqueue_creates_a_queued_job(self):
        job = make_job()
        self.assertEqual(job.status, ImportJob.Status.QUEUED)
        self.assertEqual(job.attempts, 0)
        self.assertIsNotNone(job.active_dedupe_key)

    def test_duplicate_source_raises_with_the_existing_job(self):
        first = make_job()
        with self.assertRaises(queue.DuplicateImport) as ctx:
            make_job()
        self.assertEqual(ctx.exception.existing.pk, first.pk)

    def test_duplicate_detection_is_case_insensitive(self):
        make_job(source_url="https://example.com/movie.mp4")
        with self.assertRaises(queue.DuplicateImport):
            make_job(source_url="https://EXAMPLE.com/movie.mp4")


class ClaimTests(TestCase):
    def test_claim_marks_the_job_active(self):
        job = make_job()
        claimed = queue.claim_next_job(worker_pid=1234)
        self.assertIsNotNone(claimed)
        claimed.refresh_from_db()
        self.assertEqual(claimed.pk, job.pk)
        self.assertEqual(claimed.status, ImportJob.Status.DOWNLOADING)
        self.assertEqual(claimed.worker_pid, 1234)
        self.assertEqual(claimed.attempts, 1)
        self.assertIsNotNone(claimed.heartbeat_at)
        self.assertIsNotNone(claimed.started_at)

    def test_claim_returns_none_when_empty(self):
        self.assertIsNone(queue.claim_next_job())

    def test_claim_never_returns_the_same_job_twice(self):
        make_job()
        first = queue.claim_next_job()
        second = queue.claim_next_job()
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_oldest_job_is_claimed_first(self):
        first = make_job(source_url="https://example.com/1.mp4")
        second = make_job(source_url="https://example.com/2.mp4")
        claimed = queue.claim_next_job()
        self.assertEqual(claimed.pk, first.pk)
        self.assertNotEqual(claimed.pk, second.pk)

    def test_concurrency_limit_is_respected(self):
        SiteSetting.objects.update_or_create(pk=1, defaults={"max_concurrent_jobs": 1})
        make_job(source_url="https://example.com/1.mp4")
        make_job(source_url="https://example.com/2.mp4")
        self.assertIsNotNone(queue.claim_next_job())
        self.assertIsNone(queue.claim_next_job())

    def test_concurrency_can_be_overridden_per_run(self):
        SiteSetting.objects.update_or_create(pk=1, defaults={"max_concurrent_jobs": 1})
        make_job(source_url="https://example.com/1.mp4")
        make_job(source_url="https://example.com/2.mp4")
        self.assertIsNotNone(queue.claim_next_job(max_concurrent=2))
        self.assertIsNotNone(queue.claim_next_job(max_concurrent=2))


class HeartbeatTests(TestCase):
    def test_heartbeat_updates_progress(self):
        job = make_job()
        queue.claim_next_job()
        queue.heartbeat(job.pk, bytes_downloaded=500, total_bytes=1000, progress_percent=50)
        job.refresh_from_db()
        self.assertEqual(job.bytes_downloaded, 500)
        self.assertEqual(job.total_bytes, 1000)
        self.assertEqual(job.progress_percent, 50)

    def test_progress_is_clamped_below_completion(self):
        job = make_job()
        queue.heartbeat(job.pk, progress_percent=500)
        job.refresh_from_db()
        self.assertEqual(job.progress_percent, 99)


class CancelTests(TestCase):
    def test_cancelling_a_queued_job_finishes_it_immediately(self):
        job = make_job()
        queue.request_cancel(job)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.CANCELLED)
        self.assertIsNone(job.active_dedupe_key)

    def test_cancelled_job_frees_the_source_for_reimport(self):
        job = make_job()
        queue.request_cancel(job)
        again = make_job()
        self.assertNotEqual(job.pk, again.pk)

    def test_cancelling_an_active_job_only_requests_it(self):
        job = make_job()
        queue.claim_next_job()
        queue.request_cancel(job)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.DOWNLOADING)
        self.assertTrue(job.cancel_requested)
        self.assertTrue(queue.is_cancel_requested(job.pk))

    def test_cancelling_a_finished_job_is_an_error(self):
        job = make_job()
        job.mark_terminal(ImportJob.Status.COMPLETED)
        with self.assertRaises(queue.QueueError):
            queue.request_cancel(job)


class RetryTests(TestCase):
    def test_failed_job_can_be_retried(self):
        job = make_job()
        queue.claim_next_job()
        job.refresh_from_db()
        job.mark_terminal(ImportJob.Status.FAILED, error_code="http_500")

        queue.retry_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.QUEUED)
        self.assertEqual(job.error_code, "")
        self.assertIsNotNone(job.active_dedupe_key)
        self.assertEqual(job.progress_percent, 0)

    def test_retry_is_refused_after_the_attempt_limit(self):
        job = make_job()
        job.attempts = 3
        job.max_attempts = 3
        job.status = ImportJob.Status.FAILED
        job.active_dedupe_key = None
        job.save(update_fields=["attempts", "max_attempts", "status", "active_dedupe_key"])
        with self.assertRaises(queue.QueueError):
            queue.retry_job(job)

    def test_retry_is_refused_when_another_active_job_exists(self):
        job = make_job()
        queue.claim_next_job()
        job.refresh_from_db()
        job.mark_terminal(ImportJob.Status.FAILED)
        newer = make_job()  # occupies the dedupe slot

        with self.assertRaises(queue.DuplicateImport) as ctx:
            queue.retry_job(job)
        self.assertEqual(ctx.exception.existing.pk, newer.pk)

    def test_retrying_a_running_job_is_an_error(self):
        job = make_job()
        with self.assertRaises(queue.QueueError):
            queue.retry_job(job)


class StaleRecoveryTests(TestCase):
    def test_stale_active_job_is_failed(self):
        job = make_job()
        queue.claim_next_job()
        ImportJob.objects.filter(pk=job.pk).update(
            heartbeat_at=timezone.now() - timedelta(hours=1)
        )
        recovered = queue.recover_stale_jobs(stale_seconds=600)
        self.assertEqual(len(recovered), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.FAILED)
        self.assertEqual(job.error_code, "stale_worker")
        self.assertIsNone(job.active_dedupe_key)

    def test_fresh_job_is_left_alone(self):
        make_job()
        queue.claim_next_job()
        self.assertEqual(queue.recover_stale_jobs(stale_seconds=600), [])

    def test_queued_jobs_are_not_treated_as_stale(self):
        make_job()
        self.assertEqual(queue.recover_stale_jobs(stale_seconds=600), [])

    def test_recovered_job_allows_reimport_of_the_source(self):
        job = make_job()
        queue.claim_next_job()
        ImportJob.objects.filter(pk=job.pk).update(
            heartbeat_at=timezone.now() - timedelta(hours=1)
        )
        queue.recover_stale_jobs(stale_seconds=600)
        again = make_job()
        self.assertNotEqual(again.pk, job.pk)


class NonceHousekeepingTests(TestCase):
    def test_expired_nonces_are_purged(self):
        ApiNonce.objects.create(
            key_id="k", nonce="old", expires_at=timezone.now() - timedelta(minutes=1)
        )
        ApiNonce.objects.create(
            key_id="k", nonce="live", expires_at=timezone.now() + timedelta(minutes=5)
        )
        removed = queue.purge_expired_nonces()
        self.assertEqual(removed, 1)
        self.assertEqual(ApiNonce.objects.count(), 1)
