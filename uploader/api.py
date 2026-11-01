"""
Blog-facing JSON API (brief sections 8 and 15).

Every endpoint is HMAC-authenticated (see :mod:`uploader.services.api_auth`) and
CSRF-exempt: this API is for server-to-server calls, not browsers, so there is no
session to protect and a shared-secret signature is the actual control.

Blueprint:

    POST /api/v1/imports/                    create an import job
    GET  /api/v1/imports/<public_id>/        job status + result
    POST /api/v1/imports/<public_id>/cancel/ cancel (queued: immediate)
    POST /api/v1/imports/<public_id>/retry/  requeue a failed/cancelled job
    POST /api/v1/filenames/preview/          auto/manual filename proposals

Responses are deliberately small and stable; the blog stores only the public id,
the download-page URL and the filename.
"""

from __future__ import annotations

import json
from functools import wraps
from urllib.parse import urlsplit

from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from uploader.models import ImportJob, JobLog, SiteSetting, Video
from uploader.services import naming, queue
from uploader.services.api_auth import ApiAuthError, authenticate

API_RATE_LIMIT = 120  # requests per key per minute
API_RATE_WINDOW = 60


def _error(code: str, message: str = "", status: int = 400) -> JsonResponse:
    return JsonResponse({"error": code, "message": message or code}, status=status)


def _json_body(request) -> dict:
    try:
        payload = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Body must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Body must be a JSON object.")
    return payload


def _rate_limited(key_id: str) -> bool:
    """
    Fixed-window per-key limiter.

    Per *key*, not per IP: the caller is a server, and a shared cPanel IP would
    otherwise throttle unrelated sites.
    """
    cache_key = f"api-rate:{key_id}"
    current = cache.get(cache_key, 0)
    if current >= API_RATE_LIMIT:
        return True
    if current == 0:
        cache.set(cache_key, 1, API_RATE_WINDOW)
    else:
        cache.incr(cache_key)
    return False


def api_endpoint(scope: str):
    """Authenticate, rate-limit, then run the view."""

    def decorator(view):
        @csrf_exempt
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            try:
                caller = authenticate(request, required_scope=scope)
            except ApiAuthError as exc:
                status = 401 if exc.code != "forbidden_scope" else 403
                return _error(exc.code, exc.message, status=status)

            if _rate_limited(caller.client.key_id):
                return _error("rate_limited", "Too many requests.", status=429)

            request.api_caller = caller
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def _video_payload(video: Video | None) -> dict | None:
    if video is None:
        return None
    return {
        "public_id": video.public_id,
        "filename": video.stored_filename,
        "size_bytes": video.size_bytes,
        "sha256": video.sha256,
        "extension": video.extension,
        "duration_seconds": video.duration_seconds,
        "width": video.width,
        "height": video.height,
        "status": video.status,
        "download_page_url": video.download_url,
        "thumbnail_available": bool(video.thumbnail_path),
    }


def _job_payload(job: ImportJob, *, include_logs: bool = False) -> dict:
    payload = {
        "id": job.public_id,
        "status": job.status,
        "progress_percent": job.progress_percent,
        "bytes_downloaded": job.bytes_downloaded,
        "total_bytes": job.total_bytes,
        "filename": job.final_filename,
        "requested_filename": job.requested_filename,
        "filename_mode": job.filename_mode,
        "error": (
            {"code": job.error_code, "message": job.error_message} if job.error_code else None
        ),
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "video": _video_payload(job.video) if job.video_id else None,
    }
    if include_logs:
        payload["logs"] = [
            {"level": entry.level, "message": entry.message, "at": entry.created_at.isoformat()}
            for entry in JobLog.objects.filter(job=job).order_by("-created_at")[:20]
        ]
    return payload


def _looks_like_http_url(value: str) -> bool:
    parts = urlsplit(value or "")
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


@api_endpoint("import")
def create_import(request):
    if request.method != "POST":
        return _error("method_not_allowed", "POST only.", status=405)
    try:
        payload = _json_body(request)
    except ValueError as exc:
        return _error("bad_json", str(exc))

    source_url = str(payload.get("source_url") or "").strip()
    if not source_url:
        return _error("missing_source_url", "source_url is required.")
    if not _looks_like_http_url(source_url):
        return _error("invalid_source_url", "source_url must be an absolute http(s) URL.")

    filename_mode = str(payload.get("filename_mode") or ImportJob.FilenameMode.AUTO)
    if filename_mode not in dict(ImportJob.FilenameMode.choices):
        return _error("invalid_filename_mode", "Use 'auto' or 'manual'.")
    if filename_mode == ImportJob.FilenameMode.MANUAL and not payload.get("requested_filename"):
        return _error("missing_filename", "requested_filename is required in manual mode.")

    url_type = str(payload.get("url_type") or ImportJob.UrlType.DIRECT)
    if url_type not in dict(ImportJob.UrlType.choices):
        return _error("invalid_url_type", "Use 'direct' or 'provider'.")

    try:
        job = queue.enqueue_import(
            source_url=source_url,
            title_hint=str(payload.get("title") or ""),
            filename_mode=filename_mode,
            requested_filename=str(payload.get("requested_filename") or ""),
            created_by=str(payload.get("created_by") or "")[:150],
            url_type=url_type,
            provider=str(payload.get("provider") or "")[:40],
        )
    except queue.DuplicateImport as exc:
        return JsonResponse(
            {"error": "duplicate_import", "message": str(exc), "job": _job_payload(exc.existing)},
            status=409,
        )
    except queue.QueueError as exc:
        return _error("queue_error", str(exc))

    return JsonResponse({"job": _job_payload(job)}, status=201)


@api_endpoint("status")
def import_status(request, public_id: str):
    if request.method != "GET":
        return _error("method_not_allowed", "GET only.", status=405)
    job = (
        ImportJob.objects.filter(public_id=public_id).select_related("video").first()
    )
    if job is None:
        return _error("not_found", "No such import.", status=404)
    return JsonResponse({"job": _job_payload(job, include_logs=True)})


@api_endpoint("import")
def cancel_import(request, public_id: str):
    if request.method != "POST":
        return _error("method_not_allowed", "POST only.", status=405)
    job = ImportJob.objects.filter(public_id=public_id).first()
    if job is None:
        return _error("not_found", "No such import.", status=404)
    try:
        queue.request_cancel(job)
    except queue.QueueError as exc:
        return _error("not_cancellable", str(exc), status=409)
    job.refresh_from_db()
    return JsonResponse({"job": _job_payload(job)})


@api_endpoint("import")
def retry_import(request, public_id: str):
    if request.method != "POST":
        return _error("method_not_allowed", "POST only.", status=405)
    job = ImportJob.objects.filter(public_id=public_id).first()
    if job is None:
        return _error("not_found", "No such import.", status=404)
    try:
        queue.retry_job(job)
    except queue.DuplicateImport as exc:
        return JsonResponse(
            {"error": "duplicate_import", "message": str(exc), "job": _job_payload(exc.existing)},
            status=409,
        )
    except queue.QueueError as exc:
        return _error("not_retryable", str(exc), status=409)
    job.refresh_from_db()
    return JsonResponse({"job": _job_payload(job)})


@api_endpoint("import")
def preview_filename(request):
    """
    Filename proposals shown in the blog's import panel.

    The preview never touches storage: it only applies the same cleaning rules so
    the administrator sees the exact final name before the import starts.
    """
    if request.method != "POST":
        return _error("method_not_allowed", "POST only.", status=405)
    try:
        payload = _json_body(request)
    except ValueError as exc:
        return _error("bad_json", str(exc))

    original = str(payload.get("original_filename") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not original:
        return _error("missing_original_filename", "original_filename is required.")

    setting = SiteSetting.load()
    from uploader.services.pipeline import allowed_extensions, brand_rules

    preview = naming.preview_variants(
        original,
        title,
        manual_name=str(payload.get("manual_name") or ""),
        brand_rules=brand_rules(),
        brand_suffix=setting.brand_suffix,
        allowed_extensions=allowed_extensions(setting),
    )
    return JsonResponse(preview)
