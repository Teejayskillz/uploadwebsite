"""
Data model for the NZDWorld storage / uploader service.

Tables are grouped by purpose:

* configuration .............. SiteSetting, BrandRule, AdPlacement
* storage .................... Video
* queue ...................... ImportJob, JobLog
* delivery ................... DownloadSession, DownloadEvent
* access ..................... ApiClient, ApiNonce, PremiumAssertion
* accountability ............. AuditLog

Two deliberate choices worth knowing before editing this file:

1. ``ImportJob.active_dedupe_key`` is a *nullable unique* column. MySQL/MariaDB
   treat every NULL as distinct, so "at most one active job per source URL" is
   enforced by the database itself while finished jobs keep their history. This
   is how duplicate concurrent imports are prevented without partial indexes
   (which MariaDB does not support).
2. No model stores a filesystem path outside ``Video.relative_path``, and that
   path is always interpreted relative to ``settings.VIDEO_ROOT`` by the storage
   service, never taken from a request.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone


def new_public_id() -> str:
    """Opaque, unguessable identifier used in URLs instead of database PKs."""
    return uuid.uuid4().hex


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
class SiteSetting(models.Model):
    """
    Singleton row (pk=1) holding every admin-tunable limit from brief section 16.

    Read through ``SiteSetting.load()`` so callers never have to handle a
    missing row or a race on first creation.
    """

    max_concurrent_jobs = models.PositiveSmallIntegerField(
        default=1, help_text="How many imports may run at the same time."
    )
    max_file_size_bytes = models.BigIntegerField(
        default=4 * 1024**3, help_text="Reject any single video larger than this."
    )
    allowed_formats = models.JSONField(
        default=list,
        blank=True,
        help_text='Lower-case extensions without dots, e.g. ["mp4", "mkv", "webm"].',
    )
    download_token_ttl_minutes = models.PositiveIntegerField(
        default=30, help_text="Lifetime of a signed direct-download URL."
    )
    low_storage_warn_percent = models.PositiveSmallIntegerField(
        default=85, help_text="Warn when this percentage of the storage budget is used."
    )
    low_storage_reserve_bytes = models.BigIntegerField(
        default=2 * 1024**3, help_text="Refuse new imports below this much free space."
    )
    queue_max_attempts = models.PositiveSmallIntegerField(default=3)
    queue_retry_backoff_seconds = models.PositiveIntegerField(default=60)
    ads_enabled_globally = models.BooleanField(default=True)
    premium_assertion_ttl_minutes = models.PositiveIntegerField(
        default=15, help_text="How long a blog premium-verification result is trusted."
    )
    brand_suffix = models.CharField(
        max_length=64,
        default="NZDWorld.com",
        help_text="Appended to every stored filename, after ' | '.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Site settings"
        verbose_name_plural = "Site settings"

    def __str__(self) -> str:
        return "Site settings"

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce the singleton
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "SiteSetting":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class BrandRule(TimeStampedModel):
    """
    One configurable source-brand removal rule (brief section 5/16).

    Rules are applied only to whole separator-delimited tokens, so a rule for
    ``nkiri`` can never mangle a legitimate title that happens to contain that
    substring. ``kind`` exists so domain-shaped tokens can be stripped generally
    while specific site names are listed explicitly.
    """

    class Kind(models.TextChoices):
        DOMAIN = "domain", "Domain-shaped token (site.com)"
        TOKEN = "token", "Exact site/brand token"
        REGEX = "regex", "Regular expression"

    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.TOKEN)
    pattern = models.CharField(
        max_length=255,
        help_text="For 'token': the literal token. For 'regex': a Python regex. "
        "For 'domain': unused (the generic domain rule applies).",
    )
    replacement = models.CharField(max_length=64, blank=True, default="")
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["display_order", "name"]
        verbose_name = "Brand removal rule"
        verbose_name_plural = "Brand removal rules"

    def __str__(self) -> str:
        return f"{self.name} ({self.get_kind_display()})"


class AdPlacement(TimeStampedModel):
    """Advertisement slot on a download page (brief section 13)."""

    class Slot(models.TextChoices):
        HEADER = "header", "Header"
        ABOVE_BUTTON = "above_button", "Above download button"
        BELOW_BUTTON = "below_button", "Below download button"
        SIDEBAR = "sidebar", "Sidebar"

    name = models.CharField(max_length=100)
    slot = models.CharField(max_length=20, choices=Slot.choices, default=Slot.HEADER)
    html_code = models.TextField(
        blank=True,
        default="",
        help_text="HTML/ad-network snippet. Rendered without Django escaping; "
        "only staff may edit it.",
    )
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["slot", "display_order"]
        verbose_name = "Ad placement"
        verbose_name_plural = "Ad placements"

    def __str__(self) -> str:
        return f"{self.name} ({self.get_slot_display()})"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
class Video(TimeStampedModel):
    """
    One stored video file.

    ``relative_path`` is the only place a location is recorded, and it is always
    relative to ``settings.VIDEO_ROOT``. The public surface is ``public_id``;
    paths are never exposed to visitors.
    """

    class Status(models.TextChoices):
        READY = "ready", "Ready"
        PROCESSING = "processing", "Processing"
        MISSING = "missing", "File missing"
        QUARANTINED = "quarantined", "Quarantined"
        DELETED = "deleted", "Deleted"

    class MetadataStatus(models.TextChoices):
        NONE = "none", "Not requested"
        EMBEDDED = "embedded", "Embedded and verified"
        PARTIAL = "partial", "Partially embedded (container limits)"
        UNSUPPORTED = "unsupported", "Container cannot carry these fields"
        FAILED = "failed", "Embedding failed"

    public_id = models.CharField(
        max_length=32, unique=True, default=new_public_id, editable=False
    )
    title = models.CharField(max_length=255)
    original_filename = models.CharField(max_length=255)
    stored_filename = models.CharField(max_length=255)
    relative_path = models.CharField(max_length=512, unique=True)
    extension = models.CharField(max_length=16, db_index=True)
    mime_type = models.CharField(max_length=128, blank=True, default="")

    size_bytes = models.BigIntegerField(default=0, db_index=True)
    sha256 = models.CharField(max_length=64, blank=True, default="", db_index=True)

    duration_seconds = models.FloatField(null=True, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    frame_rate = models.FloatField(null=True, blank=True)
    video_codec = models.CharField(max_length=64, blank=True, default="")
    audio_codec = models.CharField(max_length=64, blank=True, default="")
    stream_count = models.PositiveSmallIntegerField(default=0)
    streams = models.JSONField(
        default=list, blank=True, help_text="Compact per-stream summary from ffprobe."
    )
    probe = models.JSONField(
        default=dict, blank=True, help_text="Verbatim ffprobe format section."
    )

    source_url = models.TextField(blank=True, default="")
    source_type = models.CharField(max_length=20, blank=True, default="direct")
    download_page_url = models.CharField(max_length=500, blank=True, default="")

    metadata_status = models.CharField(
        max_length=20, choices=MetadataStatus.choices, default=MetadataStatus.NONE
    )
    metadata_fields = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-field result: requested, written and verified flags.",
    )
    metadata_embedded_at = models.DateTimeField(null=True, blank=True)
    thumbnail_path = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text="Poster frame path, relative to VIDEO_ROOT. Empty when none was made.",
    )
    thumbnail_at_seconds = models.FloatField(
        null=True, blank=True, help_text="Timestamp the poster frame was taken from."
    )

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.READY, db_index=True
    )
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["sha256", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.stored_filename} ({self.public_id[:8]})"

    @property
    def is_available(self) -> bool:
        return self.status == Video.Status.READY and self.deleted_at is None

    @property
    def download_url(self) -> str:
        from django.conf import settings

        return f"{settings.SITE_URL}/d/{self.public_id}/"


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------
class ImportJob(TimeStampedModel):
    """
    Persistent job record for one import attempt.

    Claiming, cancellation and stale recovery all hinge on ``status`` plus
    ``heartbeat_at``; see ``uploader.services.queue``.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        DOWNLOADING = "downloading", "Downloading"
        TRANSFERRING = "transferring", "Transferring"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    ACTIVE_STATUSES = (
        Status.DOWNLOADING,
        Status.TRANSFERRING,
        Status.PROCESSING,
    )
    TERMINAL_STATUSES = (
        Status.COMPLETED,
        Status.FAILED,
        Status.CANCELLED,
    )

    class UrlType(models.TextChoices):
        DIRECT = "direct", "Direct video URL"
        PROVIDER = "provider", "Third-party file host"

    class FilenameMode(models.TextChoices):
        AUTO = "auto", "Automatic"
        MANUAL = "manual", "Manual"

    public_id = models.CharField(
        max_length=32, unique=True, default=new_public_id, editable=False
    )

    source_url = models.TextField()
    url_type = models.CharField(
        max_length=20, choices=UrlType.choices, default=UrlType.DIRECT
    )
    provider = models.CharField(
        max_length=40,
        blank=True,
        default="",
        help_text="Adapter key when url_type=provider (e.g. 'wildshare').",
    )
    title_hint = models.CharField(max_length=255, blank=True, default="")

    filename_mode = models.CharField(
        max_length=10, choices=FilenameMode.choices, default=FilenameMode.AUTO
    )
    requested_filename = models.CharField(max_length=255, blank=True, default="")
    final_filename = models.CharField(max_length=255, blank=True, default="")

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    progress_percent = models.PositiveSmallIntegerField(default=0)
    bytes_downloaded = models.BigIntegerField(default=0)
    total_bytes = models.BigIntegerField(null=True, blank=True)

    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)

    # See module docstring: nullable unique enforces "one active job per source".
    active_dedupe_key = models.CharField(
        max_length=64, unique=True, null=True, blank=True, editable=False
    )
    dedupe_key = models.CharField(max_length=64, blank=True, default="", db_index=True)

    cancel_requested = models.BooleanField(default=False)
    worker_pid = models.IntegerField(null=True, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    created_by = models.CharField(
        max_length=150, blank=True, default="", help_text="Blog admin username."
    )
    video = models.OneToOneField(
        Video,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="import_job",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["status", "heartbeat_at"]),
        ]

    def __str__(self) -> str:
        return f"#{self.pk} {self.status} {self.source_url[:60]}"

    @property
    def is_active(self) -> bool:
        return self.status in self.ACTIVE_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES

    def mark_terminal(self, status: str, *, error_code: str = "", error_message: str = ""):
        """Finish a job and release its dedupe slot in one atomic update."""
        self.status = status
        self.error_code = error_code[:64]
        self.error_message = error_message[:4000]
        self.finished_at = timezone.now()
        self.heartbeat_at = self.finished_at
        self.active_dedupe_key = None
        self.progress_percent = 100 if status == self.Status.COMPLETED else self.progress_percent
        self.save(
            update_fields=[
                "status",
                "error_code",
                "error_message",
                "finished_at",
                "heartbeat_at",
                "active_dedupe_key",
                "progress_percent",
                "updated_at",
            ]
        )


class JobLog(models.Model):
    """Bounded, human-readable job history shown in the admin queue view."""

    class Level(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        ERROR = "error", "Error"

    job = models.ForeignKey(ImportJob, on_delete=models.CASCADE, related_name="logs")
    level = models.CharField(max_length=10, choices=Level.choices, default=Level.INFO)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Job log entry"
        verbose_name_plural = "Job log entries"

    def __str__(self) -> str:
        return f"{self.level}: {self.message[:60]}"


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
class DownloadSession(models.Model):
    """
    One visitor's download intent.

    A session is created when the visitor presses "download" on the download
    page and owns the signed token that follows. Range requests increment
    ``range_requests``; they are never counted as new downloads.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"
        EXPIRED = "expired", "Expired"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    public_id = models.CharField(
        max_length=32, unique=True, default=new_public_id, editable=False
    )
    video = models.ForeignKey(Video, on_delete=models.PROTECT, related_name="sessions")
    is_premium = models.BooleanField(default=False)
    premium_subject = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text="Opaque blog account identifier; never a trust signal on its own.",
    )
    visitor_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    user_agent = models.CharField(max_length=300, blank=True, default="")
    referrer = models.CharField(max_length=500, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    bytes_served = models.BigIntegerField(default=0)
    range_requests = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["video", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.video.public_id[:8]} session {self.public_id[:8]}"

    def is_valid(self) -> bool:
        return self.status == self.Status.ACTIVE and self.expires_at > timezone.now()


class DownloadEvent(models.Model):
    """
    Fine-grained delivery record used for analytics.

    ``kind`` separates a real download from a Range continuation so counts stay
    honest (brief section 14).
    """

    class Kind(models.TextChoices):
        SESSION_STARTED = "session_started", "Session started"
        TOKEN_ISSUED = "token_issued", "Token issued"
        BYTES_SERVED = "bytes_served", "Bytes served"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        TOKEN_EXPIRED = "token_expired", "Token expired"
        TOKEN_INVALID = "token_invalid", "Token invalid"

    session = models.ForeignKey(
        DownloadSession,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="events",
    )
    video = models.ForeignKey(
        Video,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="events",
        help_text="Null only for an invalid token that could not be traced to a video.",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    bytes_count = models.BigIntegerField(default=0)
    is_range_request = models.BooleanField(default=False)
    detail = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["video", "kind", "-created_at"]),
            models.Index(fields=["kind", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.video_id}"


# ---------------------------------------------------------------------------
# Machine access (blog -> uploader)
# ---------------------------------------------------------------------------
class ApiClient(models.Model):
    """A trusted server caller (currently the main blog)."""

    name = models.CharField(max_length=100, unique=True)
    key_id = models.CharField(max_length=32, unique=True, db_index=True)
    secret_hash = models.CharField(
        max_length=64, help_text="SHA-256 hex of the secret; the secret itself is never stored."
    )
    permissions = models.JSONField(
        default=list,
        blank=True,
        help_text='Allowed scopes, e.g. ["import", "status", "premium"]',
    )
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    request_count = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.key_id})"

    def allows(self, scope: str) -> bool:
        return self.is_active and (scope in self.permissions or "*" in self.permissions)


class ApiNonce(models.Model):
    """
    Replay protection for signed API requests.

    A request must present a nonce that has not been seen before; rows expire
    and are pruned by the same cron worker that processes the queue.
    """

    key_id = models.CharField(max_length=32, db_index=True)
    nonce = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["key_id", "nonce"], name="uploader_api_nonce_unique"
            ),
        ]
        indexes = [models.Index(fields=["expires_at"])]
        verbose_name = "API nonce"
        verbose_name_plural = "API nonces"

    def __str__(self) -> str:
        return f"{self.key_id}:{self.nonce[:12]}"


class PremiumAssertion(models.Model):
    """
    Short-lived cache of the blog's server-to-server premium answer.

    This is **not** a membership database: it only remembers, for a few minutes,
    what nzdworld.com said about one account. Expiry is enforced on every read.
    """

    subject_id = models.CharField(
        max_length=150, db_index=True, help_text="Opaque blog account identifier."
    )
    is_premium = models.BooleanField(default=False)
    verified_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    source = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["subject_id"], name="uploader_premium_subject_unique"
            ),
        ]
        indexes = [models.Index(fields=["expires_at"])]
        verbose_name = "Premium assertion"
        verbose_name_plural = "Premium assertions"

    def __str__(self) -> str:
        state = "premium" if self.is_premium else "free"
        return f"{self.subject_id} ({state})"

    def is_current(self) -> bool:
        return self.is_premium and self.expires_at > timezone.now()


# ---------------------------------------------------------------------------
# Accountability
# ---------------------------------------------------------------------------
class AuditLog(models.Model):
    """Who did what to which stored object, for destructive admin actions."""

    actor = models.CharField(max_length=150, blank=True, default="")
    action = models.CharField(max_length=64, db_index=True)
    target_type = models.CharField(max_length=64, blank=True, default="")
    target_id = models.CharField(max_length=64, blank=True, default="")
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Audit log entry"
        verbose_name_plural = "Audit log entries"

    def __str__(self) -> str:
        return f"{self.action} {self.target_type}:{self.target_id}"
