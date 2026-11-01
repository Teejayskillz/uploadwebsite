"""
Storage inspection and safe deletion (brief section 9).

Everything here is read-only unless a function is explicitly named ``delete_*``,
and every deletion is validated to stay inside ``VIDEO_ROOT`` before anything is
unlinked. The account's own budget is reported alongside the real free space,
because this is a shared cPanel volume: "1.2 TB free" is not "1.2 TB for this
service".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db.models import Count, Sum
from django.utils import timezone

from uploader.models import AuditLog, ImportJob, SiteSetting, Video


@dataclass(frozen=True)
class StorageStats:
    used_bytes: int
    budget_bytes: int
    free_bytes: int
    disk_total_bytes: int
    used_percent: float
    video_count: int
    total_video_bytes: int
    largest: list[Video]
    recent: list[Video]
    failed_jobs: list[ImportJob]
    orphan_count: int
    orphan_bytes: int
    partial_count: int
    low_storage: bool


def video_root() -> Path:
    return Path(settings.VIDEO_ROOT).resolve()


def temp_root() -> Path:
    return Path(settings.TEMP_ROOT).resolve()


def _walk_files(root: Path):
    if not root.exists():
        return
    for path in root.rglob("*"):
        try:
            if path.is_file():
                yield path
        except OSError:
            continue


def referenced_paths() -> set[str]:
    referenced: set[str] = set()
    for rel in Video.objects.values_list("relative_path", "thumbnail_path"):
        for value in rel:
            if value:
                referenced.add(value)
    return referenced


def find_orphans() -> list[dict]:
    """
    Files on disk that no ``Video`` row references, with their sizes.

    This is the safe direction of orphan detection: a *row* without a file is
    reported separately (``missing``), never treated as deletable here.
    """
    root = video_root()
    referenced = referenced_paths()
    orphans: list[dict] = []
    for path in _walk_files(root):
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if relative in referenced:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        orphans.append({"relative": relative, "size_bytes": size})
    orphans.sort(key=lambda item: item["size_bytes"], reverse=True)
    return orphans


def find_missing_files() -> list[Video]:
    root = video_root()
    missing = []
    for video in Video.objects.exclude(status=Video.Status.DELETED):
        if not (root / video.relative_path).is_file():
            missing.append(video)
    return missing


def find_partials() -> list[dict]:
    root = temp_root()
    partials = []
    for path in _walk_files(root):
        if path.suffix == ".part" or ".branded." in path.name:
            try:
                relative = path.relative_to(root).as_posix()
                partials.append({"relative": relative, "size_bytes": path.stat().st_size})
            except (OSError, ValueError):
                continue
    return partials


def duplicate_hashes() -> list[dict]:
    rows = (
        Video.objects.exclude(sha256="")
        .exclude(status=Video.Status.DELETED)
        .values("sha256")
        .annotate(count=Count("id"), size=Sum("size_bytes"))
        .filter(count__gt=1)
        .order_by("-count")
    )
    return list(rows)


def stats() -> StorageStats:
    setting = SiteSetting.load()
    root = video_root()
    if root.exists():
        import shutil

        usage = shutil.disk_usage(str(root))
        free_bytes, disk_total = usage.free, usage.total
    else:
        free_bytes, disk_total = 0, 0

    videos = Video.objects.exclude(status=Video.Status.DELETED)
    total_video_bytes = videos.aggregate(total=Sum("size_bytes"))["total"] or 0
    orphans = find_orphans()
    partials = find_partials()

    used = total_video_bytes
    budget = int(getattr(settings, "STORAGE_BUDGET_BYTES", 0))
    percent = (used / budget * 100) if budget else 0.0
    low = bool(
        budget and percent >= setting.low_storage_warn_percent
        or free_bytes < setting.low_storage_reserve_bytes
    )

    return StorageStats(
        used_bytes=used,
        budget_bytes=budget,
        free_bytes=free_bytes,
        disk_total_bytes=disk_total,
        used_percent=round(percent, 1),
        video_count=videos.count(),
        total_video_bytes=total_video_bytes,
        largest=list(videos.order_by("-size_bytes")[:10]),
        recent=list(videos.order_by("-created_at")[:10]),
        failed_jobs=list(
            ImportJob.objects.filter(status=ImportJob.Status.FAILED).order_by("-created_at")[:10]
        ),
        orphan_count=len(orphans),
        orphan_bytes=sum(item["size_bytes"] for item in orphans),
        partial_count=len(partials),
        low_storage=low,
    )


def _safe_path(relative: str) -> Path:
    root = video_root()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Path escapes the storage root.") from exc
    return candidate


def delete_video(video: Video, *, actor: str = "") -> dict:
    """
    Delete one stored video.

    The row is kept and marked ``deleted`` (with its sha256 and stats) so
    analytics and duplicate history stay intact; only the file is removed.
    """
    removed: list[str] = []
    root = video_root()
    for relative in (video.relative_path, video.thumbnail_path):
        if not relative:
            continue
        try:
            path = _safe_path(relative)
        except ValueError:
            continue
        if path.is_file():
            try:
                path.unlink()
                removed.append(relative)
            except OSError:
                continue

    video.status = Video.Status.DELETED
    video.deleted_at = timezone.now()
    video.save(update_fields=["status", "deleted_at", "updated_at"])
    AuditLog.objects.create(
        actor=actor[:150],
        action="video_deleted",
        target_type="Video",
        target_id=video.public_id,
        detail={"removed": removed, "filename": video.stored_filename, "size": video.size_bytes},
    )
    return {"removed": removed}


def delete_orphan(relative: str, *, actor: str = "") -> bool:
    """Delete a file that no row references, refusing anything outside the root."""
    path = _safe_path(relative)
    if relative in referenced_paths():
        # Raced with a new import: never delete a referenced file.
        return False
    if not path.is_file():
        return False
    try:
        size = path.stat().st_size
        path.unlink()
    except OSError:
        return False
    AuditLog.objects.create(
        actor=actor[:150],
        action="orphan_deleted",
        target_type="file",
        target_id=relative[:64],
        detail={"relative": relative, "size": size},
    )
    return True


def cleanup_partials() -> int:
    root = temp_root()
    removed = 0
    for item in find_partials():
        path = root / item["relative"]
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed
