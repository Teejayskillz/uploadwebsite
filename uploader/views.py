"""Public (unauthenticated) views that are safe to expose."""

from __future__ import annotations

import shutil
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@never_cache
@require_GET
def healthz(request):
    """
    Liveness/readiness probe.

    Reports only coarse booleans and byte counts: never hostnames, paths,
    secrets or error text, because this endpoint is world-readable.

    ``storage_ready`` exists because a missing ``VIDEO_ROOT`` used to surface as
    ``free_bytes: 0``, which looks like a full disk instead of a misconfiguration.
    """
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        db_ok = False

    video_root = Path(settings.VIDEO_ROOT)
    storage_ready = video_root.is_dir()
    free_bytes: int | None = None
    if storage_ready:
        try:
            free_bytes = shutil.disk_usage(str(video_root)).free
        except OSError:
            storage_ready = False

    tooling = {
        "ffmpeg": Path(settings.FFMPEG_BINARY).is_file(),
        "ffprobe": Path(settings.FFPROBE_BINARY).is_file(),
    }

    return JsonResponse(
        {
            "ok": db_ok and storage_ready,
            "database": db_ok,
            "storage_ready": storage_ready,
            "free_bytes": free_bytes,
            "media_tooling": tooling,
            "service": "nzdworld-uploader",
        }
    )
