"""
Worker lock and partial-cleanup tests.

Regression origin: a large import failed with
``missing_source: Media file does not exist: .../tmp/<id>.part`` because a second
cron worker started while the first was still downloading and deleted the
in-progress partial file. These tests pin both halves of the fix.
"""

from __future__ import annotations

import os
import time
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase, override_settings

from uploader.models import ImportJob
from uploader.services import pipeline
from uploader.services.worker_lock import WorkerBusy, lock_path, worker_lock
from uploader.tests.support import make_scratch_dir, remove_scratch_dir


class LockTests(TestCase):
    def setUp(self):
        self.root = make_scratch_dir()
        self.addCleanup(remove_scratch_dir, self.root)
        self.overrides = override_settings(
            VIDEO_ROOT=str(self.root / "videos"),
            TEMP_ROOT=str(self.root / "tmp"),
            WORKER_LOCK_STALE_SECONDS=1800,
        )
        self.overrides.enable()
        self.addCleanup(self.overrides.disable)

    def test_lock_is_exclusive(self):
        with worker_lock() as path:
            self.assertTrue(Path(path).exists())
            with self.assertRaises(WorkerBusy):
                with worker_lock():
                    pass
        # Released on exit.
        self.assertFalse(Path(lock_path()).exists())

    def test_lock_is_released_even_on_error(self):
        with self.assertRaises(RuntimeError):
            with worker_lock():
                raise RuntimeError("boom")
        self.assertFalse(Path(lock_path()).exists())

    @override_settings(WORKER_LOCK_STALE_SECONDS=1)
    def test_stale_lock_is_taken_over(self):
        path = lock_path()
        path.write_text("999999\n0\n", encoding="utf-8")
        old = time.time() - 10
        os.utime(path, (old, old))
        with worker_lock():
            self.assertTrue(path.exists())

    def test_lock_of_a_dead_pid_is_taken_over_immediately(self):
        path = lock_path()
        path.write_text("999999\n" + str(int(time.time())) + "\n", encoding="utf-8")
        # Fresh mtime, but the PID does not exist -> takeover.
        with worker_lock():
            self.assertTrue(path.exists())


class CleanupTests(TestCase):
    def setUp(self):
        self.root = make_scratch_dir()
        self.addCleanup(remove_scratch_dir, self.root)
        self.tmp = self.root / "tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.overrides = override_settings(
            VIDEO_ROOT=str(self.root / "videos"),
            TEMP_ROOT=str(self.tmp),
            UPLOADER={"WORKER_STALE_SECONDS": 600},
        )
        self.overrides.enable()
        self.addCleanup(self.overrides.disable)

    def _partial(self, name: str, age_seconds: int) -> Path:
        path = self.tmp / name
        path.write_bytes(b"x" * 10)
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def test_fresh_partial_is_never_deleted(self):
        path = self._partial("abc.part", age_seconds=5)
        self.assertEqual(pipeline.cleanup_partials(), 0)
        self.assertTrue(path.exists())

    def test_partial_of_an_active_job_is_never_deleted(self):
        job = ImportJob.objects.create(
            source_url="https://example.com/a.mkv", status=ImportJob.Status.DOWNLOADING
        )
        path = self._partial(f"{job.public_id}.part", age_seconds=7200)
        self.assertEqual(pipeline.cleanup_partials(), 0)
        self.assertTrue(path.exists())

    def test_abandoned_old_partial_is_removed(self):
        path = self._partial("deadbeef.part", age_seconds=7200)
        self.assertEqual(pipeline.cleanup_partials(), 1)
        self.assertFalse(path.exists())


class CommandLockTests(TestCase):
    def setUp(self):
        self.root = make_scratch_dir()
        self.addCleanup(remove_scratch_dir, self.root)
        self.overrides = override_settings(
            VIDEO_ROOT=str(self.root / "videos"),
            TEMP_ROOT=str(self.root / "tmp"),
        )
        self.overrides.enable()
        self.addCleanup(self.overrides.disable)

    def test_command_reports_busy_instead_of_processing(self):
        out = StringIO()
        with worker_lock():
            call_command("process_queue", "--max-seconds", "1", stdout=out)
        self.assertIn("skipped", out.getvalue())

    def test_command_runs_when_lock_is_free(self):
        out = StringIO()
        call_command("process_queue", "--max-seconds", "1", stdout=out)
        self.assertIn("no jobs to process", out.getvalue())
