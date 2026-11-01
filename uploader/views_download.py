"""
Public download gateway (brief sections 10, 11 and 14).

Three URLs, deliberately small:

* ``/d/<public_id>/``        the download page (info, ads, one button)
* ``/d/<public_id>/start/``  mint a session + signed link, then redirect
* ``/dl/<token>/``           verify and stream bytes (Range supported)
* ``/t/<public_id>/``        poster frame, cacheable

Nothing here exposes a filesystem path, and every byte served is re-authorised
from the token and the stored session on each request.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from uploader.models import AdPlacement, DownloadEvent, SiteSetting, Video
from uploader.services import delivery, premium

SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")


def human_size(value: int) -> str:
    size = float(value or 0)
    for unit in SIZE_UNITS:
        if size < 1024 or unit == SIZE_UNITS[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def human_duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _available_video(public_id: str) -> Video:
    video = get_object_or_404(Video, public_id=public_id)
    if not video.is_available:
        raise Http404("This video is not available.")
    return video


def _active_ads() -> dict[str, list[AdPlacement]]:
    setting = SiteSetting.load()
    if not setting.ads_enabled_globally:
        return {}
    grouped: dict[str, list[AdPlacement]] = {}
    for ad in AdPlacement.objects.filter(is_active=True).order_by("slot", "display_order"):
        grouped.setdefault(ad.slot, []).append(ad)
    return grouped


@require_GET
def download_page(request, public_id: str):
    video = _available_video(public_id)
    state = premium.resolve_premium(request)
    context = {
        "video": video,
        "is_premium": state.is_premium,
        "premium_source": state.source,
        "ads": {} if state.is_premium else _active_ads(),
        "size_label": human_size(video.size_bytes),
        "duration_label": human_duration(video.duration_seconds),
        "site_name": getattr(settings, "SITE_NAME", "NZDWorld"),
        "main_site_url": getattr(settings, "BLOG_API_BASE_URL", "https://nzdworld.com"),
        "token_ttl_minutes": int(SiteSetting.load().download_token_ttl_minutes or 30),
        "has_thumbnail": bool(video.thumbnail_path),
    }
    response = render(request, "uploader/download_page.html", context)
    premium.attach_cookie(response, state)
    response["Cache-Control"] = "no-store"
    return response


@require_POST
def start_download(request, public_id: str):
    """Create the session, then hand the browser a signed, expiring URL."""
    video = _available_video(public_id)
    state = premium.resolve_premium(request)
    session = delivery.create_session(
        video, is_premium=state.is_premium, subject=state.subject, request=request
    )
    token = delivery.issue_token(session)
    response = redirect("download_file", token=token)
    premium.attach_cookie(response, state)
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def download_file(request, token: str):
    try:
        session = delivery.verify_token(token)
    except delivery.TokenExpired:
        payload = delivery.peek_token(token)
        video = (
            Video.objects.filter(public_id=payload.video_id).first() if payload else None
        )
        if video is not None:
            delivery.record_expired(video, "token expired")
            # Brief section 10: expired links return to the page that can mint a
            # fresh one, never to a dead end.
            return redirect("download_page", public_id=video.public_id)
        return render(request, "uploader/link_invalid.html", status=410)
    except delivery.TokenInvalid as exc:
        delivery.record_invalid(None, str(exc))
        return render(request, "uploader/link_invalid.html", status=410)

    try:
        response = delivery.serve_video(request, session)
    except delivery.DeliveryError as exc:
        delivery.record_failure(session, exc.code)
        return render(
            request,
            "uploader/link_invalid.html",
            {"detail": "The stored file could not be read."},
            status=404,
        )
    if session.is_premium:
        response["Cache-Control"] = "private, no-store"
    return response


@require_GET
def serve_thumbnail(request, public_id: str):
    video = get_object_or_404(Video, public_id=public_id)
    if not video.thumbnail_path:
        raise Http404("No thumbnail.")
    root = Path(settings.VIDEO_ROOT).resolve()
    path = (root / video.thumbnail_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise Http404("No thumbnail.") from exc
    if not path.is_file():
        raise Http404("No thumbnail.")
    content_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    response = FileResponse(path.open("rb"), content_type=content_type)
    response["Cache-Control"] = "public, max-age=86400"
    return response
