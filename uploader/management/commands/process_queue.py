"""
Cron entry point for the import queue (brief section 7).

Installed crontab line (one minute, bounded run time):

    * * * * * cd ~/repositories/upload_website && \
      ~/virtualenv/repositories/upload_website/3.11/bin/python manage.py \
      process_queue --max-seconds 50 >> ~/logs/uploader-queue.log 2>&1

The command is deliberately short-lived: shared cPanel hosting does not allow
persistent workers, so "run for at most 50 seconds, then exit" is the contract.
"""

from __future__ import annotations

import os
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from uploader.models import ImportJob, JobLog
from uploader.services import pipeline, queue


class Command(BaseCommand):
    help = "Process queued video imports for a bounded amount of time."

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-seconds",
            type=int,
            default=50,
            help="Stop claiming new jobs after this many seconds (cron safety).",
        )
        parser.add_argument(
            "--max-jobs",
            type=int,
            default=10,
            help="Hard cap on jobs processed in one run.",
        )
        parser.add_argument(
            "--concurrency",
            type=int,
            default=0,
            help="Override SiteSetting.max_concurrent_jobs for this run (0 = use setting).",
        )
        parser.add_argument(
            "--skip-recovery",
            action="store_true",
            help="Do not fail jobs whose worker stopped heartbeating.",
        )

    def handle(self, *args, **options):
        from uploader.services.worker_lock import WorkerBusy, worker_lock

        started = time.monotonic()
        max_seconds = max(1, options["max_seconds"])
        max_jobs = max(1, options["max_jobs"])
        concurrency = options["concurrency"] or None
        worker_pid = os.getpid()

        # One worker at a time. A download that outlives the cron budget must not
        # be duplicated (and must not have its partial file cleaned up by the run
        # that starts 60 seconds later).
        try:
            with worker_lock():
                return self._run(
                    started=started,
                    max_seconds=max_seconds,
                    max_jobs=max_jobs,
                    concurrency=concurrency,
                    worker_pid=worker_pid,
                    options=options,
                )
        except WorkerBusy as exc:
            self.stdout.write(f"skipped: {exc}")
            return None

    def _run(self, *, started, max_seconds, max_jobs, concurrency, worker_pid, options):
        if not options["skip_recovery"]:
            stale = queue.recover_stale_jobs()
            if stale:
                self.stdout.write(f"recovered {len(stale)} stale job(s)")

        pruned = queue.purge_expired_nonces()
        if pruned:
            self.stdout.write(f"pruned {pruned} expired nonce(s)")

        removed = pipeline.cleanup_partials()
        if removed:
            self.stdout.write(f"removed {removed} abandoned partial file(s)")

        processed = 0
        while processed < max_jobs and (time.monotonic() - started) < max_seconds:
            job = queue.claim_next_job(worker_pid=worker_pid, max_concurrent=concurrency)
            if job is None:
                break

            self.stdout.write(f"processing job #{job.pk} ({job.source_url[:70]})")
            try:
                pipeline.process_job(job, worker_pid=worker_pid)
            except pipeline.PipelineError as exc:
                self._fail(job, exc.code, exc.message)
                self.stderr.write(f"job #{job.pk} failed: {exc.code}")
            except Exception as exc:  # noqa: BLE001 - a worker must never crash silently
                self._fail(job, "worker_error", f"{type(exc).__name__}: {exc}")
                self.stderr.write(f"job #{job.pk} crashed: {exc}")
            processed += 1

        if processed == 0:
            self.stdout.write("no jobs to process")
        else:
            self.stdout.write(f"processed {processed} job(s) in {time.monotonic() - started:.1f}s")

    def _fail(self, job: ImportJob, code: str, message: str) -> None:
        """Always leave a claimed job in a terminal state."""
        try:
            pipeline.discard_for(job)
            JobLog.objects.create(
                job=job,
                level=JobLog.Level.ERROR,
                message=f"{code}: {message}"[:2000],
            )
            job.refresh_from_db(fields=["status"])
            if not job.is_terminal:
                job.mark_terminal(ImportJob.Status.FAILED, error_code=code, error_message=message)
        except Exception:  # pragma: no cover - last-resort safety
            # If even this fails, the stale-recovery pass will catch the job.
            pass
