"""
Database-backed import queue (brief section 7).

Why a queue at all, given cron?
-------------------------------
Shared cPanel hosting has no Redis, no Celery and no persistent daemon, but the
audit proved cron runs every minute. So the queue is a table, the worker is a
short-lived management command, and correctness comes from the database:

* **Claiming** happens inside one transaction with ``SELECT ... FOR UPDATE
  SKIP LOCKED`` (MariaDB 10.11 supports it), then the row's status is flipped to
  ``downloading`` before commit. Two workers cannot claim the same job.
* **Concurrency** is capped by counting active jobs inside the same transaction.
* **Stale recovery** uses ``heartbeat_at``: a worker that dies leaves a job in an
  active state, and the next run fails it with ``stale_worker`` and cleans up.
* **Duplicate imports** are blocked by the nullable-unique
  ``active_dedupe_key`` column rather than by a read-then-write check, so the
  guarantee survives concurrent requests.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone

from uploader.models import ApiNonce, ImportJob, SiteSetting

ACTIVE_STATUSES = ImportJob.ACTIVE_STATUSES
DEFAULT_PORTS = {"http": 80, "https": 443}


class DuplicateImport(Exception):
    """An active job for the same source already exists."""

    def __init__(self, existing: ImportJob):
        super().__init__("An import for this source is already queued or running.")
        self.existing = existing


class QueueError(Exception):
    """Invalid queue operation (wrong state, exhausted retries, ...)."""


def normalise_source_url(url: str) -> str:
    """
    Canonical form used for duplicate detection.

    Lower-cases the scheme/host, drops the default port and the fragment, and
    keeps the path/query (they are part of a signed video URL). Two spellings of
    the same URL must produce the same dedupe key.
    """
    raw = (url or "").strip()
    if not raw:
        raise QueueError("Source URL is empty.")
    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if not scheme or not host:
        raise QueueError("Source URL is not absolute.")
    port = parts.port
    if port and port != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def compute_dedupe_key(url: str) -> str:
    return hashlib.sha256(normalise_source_url(url).encode("utf-8")).hexdigest()


def active_job_count() -> int:
    return ImportJob.objects.filter(status__in=ACTIVE_STATUSES).count()


def _concurrency_limit(override: int | None = None) -> int:
    if override is not None:
        return max(1, int(override))
    return max(1, int(SiteSetting.load().max_concurrent_jobs or 1))


def enqueue_import(
    *,
    source_url: str,
    title_hint: str = "",
    filename_mode: str = ImportJob.FilenameMode.AUTO,
    requested_filename: str = "",
    created_by: str = "",
    url_type: str = ImportJob.UrlType.DIRECT,
    provider: str = "",
) -> ImportJob:
    """
    Create a queued job, or raise :class:`DuplicateImport`.

    The uniqueness of ``active_dedupe_key`` is what makes this safe under
    concurrency: the second caller loses at the database, not in Python.
    """
    key = compute_dedupe_key(source_url)
    job = ImportJob(
        source_url=source_url.strip(),
        url_type=url_type,
        provider=provider or "",
        title_hint=title_hint.strip(),
        filename_mode=filename_mode,
        requested_filename=requested_filename.strip(),
        created_by=created_by,
        dedupe_key=key,
        active_dedupe_key=key,
    )
    try:
        with transaction.atomic():
            job.save()
    except IntegrityError:
        existing = (
            ImportJob.objects.filter(active_dedupe_key=key).order_by("-created_at").first()
        )
        if existing is None:  # pragma: no cover - defensive
            raise QueueError("Duplicate insert but no active job found.") from None
        raise DuplicateImport(existing) from None
    return job


def claim_next_job(
    *, worker_pid: int | None = None, max_concurrent: int | None = None
) -> ImportJob | None:
    """
    Atomically claim the oldest queued job, or return ``None``.

    ``None`` is the normal answer when the queue is empty *or* when the active
    limit has been reached; callers must not treat it as an error.
    """
    limit = _concurrency_limit(max_concurrent)
    now = timezone.now()

    with transaction.atomic():
        if ImportJob.objects.filter(status__in=ACTIVE_STATUSES).count() >= limit:
            return None

        queryset = ImportJob.objects.filter(status=ImportJob.Status.QUEUED).order_by(
            "created_at", "pk"
        )
        if connection.features.has_select_for_update:
            lock_kwargs = {}
            if connection.features.has_select_for_update_skip_locked:
                lock_kwargs["skip_locked"] = True
            queryset = queryset.select_for_update(**lock_kwargs)

        job = queryset.first()
        if job is None:
            return None

        job.status = ImportJob.Status.DOWNLOADING
        job.worker_pid = worker_pid
        job.locked_at = now
        job.heartbeat_at = now
        job.started_at = job.started_at or now
        job.progress_percent = max(job.progress_percent, 1)
        job.attempts = (job.attempts or 0) + 1
        job.save(
            update_fields=[
                "status",
                "worker_pid",
                "locked_at",
                "heartbeat_at",
                "started_at",
                "progress_percent",
                "attempts",
                "updated_at",
            ]
        )
        return job


def heartbeat(
    job_id: int,
    *,
    bytes_downloaded: int | None = None,
    total_bytes: int | None = None,
    progress_percent: int | None = None,
    status: str | None = None,
) -> None:
    """Extend the job lease and optionally persist progress. Cheap single UPDATE."""
    fields: dict = {"heartbeat_at": timezone.now()}
    if bytes_downloaded is not None:
        fields["bytes_downloaded"] = bytes_downloaded
    if total_bytes is not None:
        fields["total_bytes"] = total_bytes
    if progress_percent is not None:
        fields["progress_percent"] = max(0, min(99, int(progress_percent)))
    if status is not None:
        fields["status"] = status
    ImportJob.objects.filter(pk=job_id).update(**fields)


def is_cancel_requested(job_id: int) -> bool:
    return bool(
        ImportJob.objects.filter(pk=job_id, cancel_requested=True).values_list("pk", flat=True).first()
    )


def request_cancel(job: ImportJob) -> ImportJob:
    """
    Cancel a job.

    Queued jobs are cancelled immediately; active jobs are only *asked* to stop,
    because the worker owns the partial file and must delete it itself.
    """
    # Always act on the stored state: callers may hold a stale instance.
    job.refresh_from_db(fields=["status", "cancel_requested"])
    if job.status == ImportJob.Status.QUEUED:
        job.mark_terminal(ImportJob.Status.CANCELLED, error_code="cancelled_by_admin")
    elif job.status in ACTIVE_STATUSES:
        ImportJob.objects.filter(pk=job.pk).update(cancel_requested=True)
        job.refresh_from_db()
    elif job.is_terminal:
        raise QueueError("This job has already finished.")
    return job


def retry_job(job: ImportJob, *, max_attempts: int | None = None) -> ImportJob:
    """
    Put a failed/cancelled job back in the queue.

    Re-acquiring the dedupe slot can fail if a newer active job exists for the
    same source; that raises :class:`DuplicateImport` instead of silently
    creating a second concurrent import.
    """
    if not job.is_terminal:
        raise QueueError("Only finished jobs can be retried.")
    limit = max_attempts if max_attempts is not None else (job.max_attempts or 3)
    if job.attempts >= limit:
        raise QueueError(f"Retry limit reached ({job.attempts}/{limit}).")

    job.status = ImportJob.Status.QUEUED
    job.error_code = ""
    job.error_message = ""
    job.progress_percent = 0
    job.bytes_downloaded = 0
    job.total_bytes = None
    job.cancel_requested = False
    job.finished_at = None
    job.locked_at = None
    job.heartbeat_at = None
    job.worker_pid = None
    job.active_dedupe_key = job.dedupe_key
    try:
        with transaction.atomic():
            job.save(
                update_fields=[
                    "status",
                    "error_code",
                    "error_message",
                    "progress_percent",
                    "bytes_downloaded",
                    "total_bytes",
                    "cancel_requested",
                    "finished_at",
                    "locked_at",
                    "heartbeat_at",
                    "worker_pid",
                    "active_dedupe_key",
                    "updated_at",
                ]
            )
    except IntegrityError:
        job.refresh_from_db()
        existing = (
            ImportJob.objects.filter(active_dedupe_key=job.dedupe_key)
            .exclude(pk=job.pk)
            .order_by("-created_at")
            .first()
        )
        if existing is None:  # pragma: no cover - defensive
            raise QueueError("Retry conflicted with another active job.") from None
        raise DuplicateImport(existing) from None
    return job


def recover_stale_jobs(*, stale_seconds: int = 600) -> list[ImportJob]:
    """
    Fail jobs whose worker stopped heartbeating.

    This is what makes an interrupted cron run safe: the partial file is removed
    by the worker's own cleanup path, and here the row is moved to a terminal
    state so the source can be imported again.
    """
    cutoff = timezone.now() - timedelta(seconds=max(60, int(stale_seconds)))
    stale = list(
        ImportJob.objects.filter(status__in=ACTIVE_STATUSES).filter(
            Q(heartbeat_at__lt=cutoff) | Q(heartbeat_at__isnull=True)
        )
    )
    recovered: list[ImportJob] = []
    for job in stale:
        job.mark_terminal(
            ImportJob.Status.FAILED,
            error_code="stale_worker",
            error_message=(
                "The worker stopped responding (no heartbeat). Partial data was discarded."
            ),
        )
        recovered.append(job)
    return recovered


def purge_expired_nonces() -> int:
    """Housekeeping: drop replay-protection rows that can no longer be valid."""
    count, _ = ApiNonce.objects.filter(expires_at__lt=timezone.now()).delete()
    return count
