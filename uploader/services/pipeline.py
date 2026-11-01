"""
Job pipeline: take a claimed :class:`ImportJob` and turn it into a stored
:class:`Video` (brief sections 4, 5, 6 and 7).

Phase 2 implements the transfer half end to end: validate -> capacity check ->
download with heartbeat/cancel -> content proof -> duplicate detection ->
filename branding -> atomic move -> ``Video`` row. Metadata embedding is Phase 3
and slots in at the marked point without changing the surrounding contract.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from django.conf import settings
from django.utils import timezone

from uploader.models import BrandRule, ImportJob, JobLog, SiteSetting, Video
from uploader.services import media, naming, queue
from uploader.services.downloader import (
    DownloadError,
    ensure_directory,
    fetch_to_file,
    free_bytes,
)
from uploader.services.netguard import UnsafeURLError, validate_url

logger = logging.getLogger("uploader")


def log(job: ImportJob, message: str, *, level: str = JobLog.Level.INFO) -> None:
    """Append a bounded, human-readable step to the job history."""
    JobLog.objects.create(job=job, level=level, message=message[:2000])


class PipelineError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _settings() -> SiteSetting:
    return SiteSetting.load()


def allowed_extensions(setting: SiteSetting) -> list[str]:
    formats = [str(item).lower().lstrip(".") for item in (setting.allowed_formats or [])]
    return formats or ["mp4", "mkv", "webm", "mov", "m4v", "avi"]


def brand_rules() -> list[tuple[str, str]]:
    return list(
        BrandRule.objects.filter(is_active=True)
        .order_by("display_order", "name")
        .values_list("kind", "pattern")
    )


def check_capacity(needed_bytes: int, setting: SiteSetting) -> None:
    """
    Refuse an import that cannot fit.

    ``STORAGE_BUDGET_BYTES`` is the account's own ceiling, not ``df``: this is a
    shared volume, so "1.2 T free" must never be reported as "1.2 T for us".
    """
    ensure_directory(Path(settings.VIDEO_ROOT))
    free = free_bytes(Path(settings.VIDEO_ROOT))
    reserve = int(setting.low_storage_reserve_bytes)
    if free - reserve < min(needed_bytes, setting.max_file_size_bytes):
        raise PipelineError(
            "storage_low",
            f"Not enough storage: {free} bytes free, {reserve} bytes reserved.",
        )


def _source_filename(job: ImportJob, final_url: str, container_extension: str) -> str:
    """
    Best available original filename, with the *sniffed* container as extension.

    The URL extension is only a fallback: a ``.mp4`` link can serve Matroska, and
    the file on disk must match the bytes.
    """
    candidate = ""
    if job.requested_filename:
        candidate = job.requested_filename
    if not candidate:
        path = urlsplit(final_url).path or urlsplit(job.source_url).path
        candidate = unquote(Path(path).name)
    stem, _ext = naming.split_extension(candidate) if candidate else ("", "")
    if not stem:
        stem = "video"
    return f"{stem}.{container_extension}"


def _unique_names(directory: Path, disk_name: str, display_name: str) -> tuple[Path, str]:
    """
    Return a free path and the matching display name.

    Both names get the same ``-1``/``-2`` suffix so the admin never sees two
    rows that look identical while pointing at different files.
    """
    target = directory / disk_name
    if not target.exists():
        return target, display_name

    disk_stem, disk_dot, disk_ext = disk_name.rpartition(".")
    if not disk_dot:
        disk_stem, disk_ext = disk_name, ""
    show_stem, show_dot, show_ext = display_name.rpartition(".")
    if not show_dot:
        show_stem, show_ext = display_name, ""

    counter = 1
    while True:
        candidate = directory / (
            f"{disk_stem}-{counter}.{disk_ext}" if disk_ext else f"{disk_stem}-{counter}"
        )
        if not candidate.exists():
            shown = (
                f"{show_stem}-{counter}.{show_ext}" if show_ext else f"{show_stem}-{counter}"
            )
            return candidate, shown
        counter += 1


def process_job(job: ImportJob, *, worker_pid: int | None = None) -> ImportJob:
    """
    Execute one claimed job.

    The caller (``process_queue``) is responsible for marking unexpected
    exceptions as failures; everything the importer itself can anticipate is
    handled here and stored on the job.
    """
    setting = _settings()
    job.worker_pid = worker_pid if worker_pid is not None else job.worker_pid
    temp_dir = Path(settings.TEMP_ROOT)
    ensure_directory(temp_dir)
    partial = temp_dir / f"{job.public_id}.part"

    if job.url_type == ImportJob.UrlType.PROVIDER:
        raise PipelineError(
            "provider_not_enabled",
            f"The '{job.provider or 'unknown'}' provider adapter is not enabled yet.",
        )

    try:
        target = validate_url(job.source_url)
    except UnsafeURLError as exc:
        raise PipelineError("unsafe_url", f"URL rejected: {exc}") from exc

    log(job, f"Validated source ({target.host}); starting download.")
    check_capacity(setting.max_file_size_bytes, setting)
    queue.heartbeat(job.pk, status=ImportJob.Status.DOWNLOADING, progress_percent=1)

    last_heartbeat = timezone.now()

    def on_progress(downloaded: int, total: int | None) -> None:
        nonlocal last_heartbeat
        percent = 0
        if total:
            percent = min(95, int(downloaded * 100 / total))
        now = timezone.now()
        # Throttle writes: progress must not turn the transfer into a DB storm.
        if (now - last_heartbeat).total_seconds() >= 5:
            last_heartbeat = now
        queue.heartbeat(
            job.pk,
            bytes_downloaded=downloaded,
            total_bytes=total,
            progress_percent=max(1, percent),
        )

    def should_cancel() -> bool:
        return queue.is_cancel_requested(job.pk)

    try:
        result = fetch_to_file(
            target,
            partial,
            max_bytes=setting.max_file_size_bytes,
            on_progress=on_progress,
            should_cancel=should_cancel,
        )
    except DownloadError as exc:
        partial.unlink(missing_ok=True)
        raise PipelineError(exc.code, exc.message) from exc

    queue.heartbeat(
        job.pk,
        status=ImportJob.Status.TRANSFERRING,
        bytes_downloaded=result.size_bytes,
        total_bytes=result.size_bytes,
        progress_percent=96,
    )
    log(job, f"Downloaded {result.size_bytes} bytes ({result.container}, sha256 {result.sha256[:12]}).")

    # --- duplicate content check (brief section 9) -------------------------
    existing = (
        Video.objects.filter(sha256=result.sha256)
        .exclude(status=Video.Status.DELETED)
        .order_by("pk")
        .first()
    )
    if existing is not None:
        partial.unlink(missing_ok=True)
        raise PipelineError(
            "duplicate_content",
            f"Identical file already stored as '{existing.stored_filename}' "
            f"(public id {existing.public_id}).",
        )

    # --- filename branding (brief section 5) ------------------------------
    original_name = _source_filename(job, result.final_url, result.extension)
    title = job.title_hint.strip() or Path(original_name).stem
    mode = job.filename_mode
    # A manual name must still describe the bytes we actually stored: if the
    # admin typed ".mkv" for an MP4 source, the container wins.
    cleaning_source = original_name
    if mode == ImportJob.FilenameMode.MANUAL and job.requested_filename:
        manual_stem, _manual_ext = naming.split_extension(job.requested_filename)
        cleaning_source = f"{manual_stem}.{result.extension}"
    try:
        cleaned = naming.clean_filename(
            cleaning_source,
            title=title,
            mode=mode,
            brand_rules=brand_rules(),
            brand_suffix=setting.brand_suffix,
            allowed_extensions=allowed_extensions(setting),
        )
    except naming.FilenameError as exc:
        partial.unlink(missing_ok=True)
        raise PipelineError("filename_error", str(exc)) from exc

    now = datetime.now()
    month_dir = Path(settings.VIDEO_ROOT) / f"{now:%Y}" / f"{now:%m}"
    ensure_directory(month_dir)
    # The branded name keeps " | NZDWorld.com" for the visitor; only the file on
    # disk is normalised, because "|" is not a legal filename character on
    # Windows and must not break a later copy/backup of the storage volume.
    try:
        disk_name = naming.disk_safe_filename(cleaned.filename)
    except naming.FilenameError as exc:
        partial.unlink(missing_ok=True)
        raise PipelineError("filename_error", str(exc)) from exc
    destination, display_name = _unique_names(month_dir, disk_name, cleaned.filename)

    # --- probe + branding metadata (brief section 6) -----------------------
    # Order matters: probe the downloaded file first (it is the only real proof
    # that this is a playable video), then write a *copy* with the NZDWorld tags,
    # verify the copy still has identical streams, and only then move anything
    # into permanent storage. The original stays untouched until verification.
    queue.heartbeat(job.pk, status=ImportJob.Status.PROCESSING, progress_percent=97)

    metadata_status = Video.MetadataStatus.NONE
    metadata_fields: dict = {}
    metadata_embedded_at = None
    source_info = None
    final_source = partial
    branded_path: Path | None = None

    tooling = media.tooling_available()
    tooling_required = bool(getattr(settings, "MEDIA_TOOLING_REQUIRED", False))

    if not tooling:
        if tooling_required:
            partial.unlink(missing_ok=True)
            raise PipelineError(
                "tooling_missing",
                "FFmpeg/FFprobe are not available to the application user.",
            )
        media_status = Video.MetadataStatus.FAILED
        metadata_fields = {"error": "tooling_missing"}
        log(job, "FFmpeg unavailable: storing the original without branding.", level=JobLog.Level.WARNING)
    else:
        try:
            source_info = media.probe(partial)
        except media.MediaToolingError as exc:
            if tooling_required:
                partial.unlink(missing_ok=True)
                raise PipelineError(exc.code, exc.message) from exc
            media_status = Video.MetadataStatus.FAILED
            metadata_fields = {"error": exc.code, "detail": exc.detail}
            log(job, f"Probe failed ({exc.code}); storing the original.", level=JobLog.Level.WARNING)

    if source_info is not None:
        requested = dict(media.BRAND_METADATA)
        requested["title"] = f"{title} | {setting.brand_suffix}"
        branded_path = partial.with_name(f"{partial.stem}.branded.{result.extension}")
        try:
            embed_result = media.embed_metadata(
                partial, branded_path, requested, extension=result.extension
            )
            metadata_fields = {
                "container": media.container_key(source_info, result.extension),
                "requested": embed_result.requested,
                "written": embed_result.written,
                "unsupported": embed_result.unsupported,
                "stream_mismatches": embed_result.mismatches,
            }
            if embed_result.mismatches:
                # Never accept a copy that changed the video itself.
                branded_path.unlink(missing_ok=True)
                if tooling_required:
                    partial.unlink(missing_ok=True)
                    raise PipelineError(
                        "stream_mismatch",
                        "Metadata copy changed the streams: " + "; ".join(embed_result.mismatches),
                    )
                metadata_status = Video.MetadataStatus.FAILED
            elif embed_result.unsupported and len(embed_result.unsupported) == len(requested):
                metadata_status = Video.MetadataStatus.UNSUPPORTED
                final_source = branded_path
            elif embed_result.unsupported:
                metadata_status = Video.MetadataStatus.PARTIAL
                final_source = branded_path
            else:
                metadata_status = Video.MetadataStatus.EMBEDDED
                final_source = branded_path
            metadata_embedded_at = timezone.now() if final_source is branded_path else None
        except media.MediaToolingError as exc:
            branded_path.unlink(missing_ok=True)
            if tooling_required:
                partial.unlink(missing_ok=True)
                raise PipelineError(exc.code, exc.message) from exc
            metadata_status = Video.MetadataStatus.FAILED
            metadata_fields = {"error": exc.code, "detail": exc.detail}
            log(job, f"Metadata embedding failed ({exc.code}); storing the original.", level=JobLog.Level.WARNING)

    # --- poster frame (brief section 6 / storage dashboard) ----------------
    thumbnail_rel = ""
    thumbnail_at = None
    if tooling and source_info is not None:
        thumb_dir = (
            Path(settings.VIDEO_ROOT)
            / str(getattr(settings, "THUMBNAIL_SUBDIR", "thumbs"))
            / f"{now:%Y}"
            / f"{now:%m}"
        )
        thumb_file = thumb_dir / f"{job.public_id}.jpg"
        try:
            thumb = media.make_thumbnail(final_source, thumb_file)
            thumbnail_rel = thumb_file.relative_to(Path(settings.VIDEO_ROOT)).as_posix()
            thumbnail_at = thumb.at_seconds
        except media.MediaToolingError as exc:
            # A missing poster frame must never lose a successfully stored video.
            log(job, f"Thumbnail failed ({exc.code}).", level=JobLog.Level.WARNING)

    queue.heartbeat(job.pk, status=ImportJob.Status.PROCESSING, progress_percent=98)
    try:
        os.replace(final_source, destination)
    except OSError as exc:
        final_source.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)
        raise PipelineError("storage_move_failed", f"Could not store the file: {exc}") from exc
    # The unbranded download is no longer needed once the verified copy is in place.
    if final_source != partial:
        partial.unlink(missing_ok=True)

    relative_path = destination.relative_to(Path(settings.VIDEO_ROOT)).as_posix()
    stored_size = destination.stat().st_size
    video = Video.objects.create(
        title=title[:255],
        original_filename=original_name[:255],
        stored_filename=display_name[:255],
        relative_path=relative_path,
        extension=result.extension,
        mime_type=f"video/{result.container}",
        size_bytes=stored_size,
        sha256=result.sha256,
        duration_seconds=source_info.duration if source_info else None,
        width=source_info.width if source_info else None,
        height=source_info.height if source_info else None,
        frame_rate=source_info.frame_rate if source_info else None,
        video_codec=source_info.video_codec if source_info else "",
        audio_codec=source_info.audio_codec if source_info else "",
        stream_count=source_info.stream_count if source_info else 0,
        streams=source_info.summary() if source_info else [],
        probe=source_info.raw_format if source_info else {},
        source_url=job.source_url,
        source_type=job.url_type,
        status=Video.Status.READY,
        metadata_status=metadata_status,
        metadata_fields=metadata_fields,
        metadata_embedded_at=metadata_embedded_at,
        thumbnail_path=thumbnail_rel,
        thumbnail_at_seconds=thumbnail_at,
    )

    job.video = video
    job.final_filename = display_name[:255]
    job.total_bytes = stored_size
    job.bytes_downloaded = result.size_bytes
    job.save(
        update_fields=[
            "video",
            "final_filename",
            "total_bytes",
            "bytes_downloaded",
            "updated_at",
        ]
    )
    log(job, f"Stored as {destination.name} (metadata: {metadata_status}).")
    job.mark_terminal(ImportJob.Status.COMPLETED)
    return job


def cleanup_partials() -> int:
    """Remove orphaned ``*.part`` files left by an interrupted worker."""
    temp_dir = Path(settings.TEMP_ROOT)
    if not temp_dir.exists():
        return 0
    removed = 0
    for path in temp_dir.glob("*.part"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def discard_for(job: ImportJob) -> None:
    """Best-effort removal of a job's partial artefacts."""
    partial = Path(settings.TEMP_ROOT) / f"{job.public_id}.part"
    partial.unlink(missing_ok=True)


def storage_report() -> dict[str, int]:
    """Bytes used by stored videos, plus the configured budget (dashboard use)."""
    root = Path(settings.VIDEO_ROOT)
    used = 0
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    used += path.stat().st_size
                except OSError:
                    continue
    budget = int(getattr(settings, "STORAGE_BUDGET_BYTES", 0))
    return {
        "used_bytes": used,
        "budget_bytes": budget,
        "free_bytes": free_bytes(root),
        "disk_total": shutil.disk_usage(str(root)).total if root.exists() else 0,
    }
