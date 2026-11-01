"""Public (unauthenticated) views that are safe to expose."""

from __future__ import annotations

import shutil

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
    """
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        db_ok = False

    try:
        usage = shutil.disk_usage(str(settings.VIDEO_ROOT))
        free_bytes = usage.free
    except OSError:
        free_bytes = 0

    return JsonResponse(
        {
            "ok": db_ok,
            "database": db_ok,
            "free_bytes": free_bytes,
            "service": "nzdworld-uploader",
        }
    )
