"""
Staff dashboards: storage management and download analytics (brief sections 9, 14).

Both pages are protected by ``staff_member_required``, which redirects to the
admin login. They never expose absolute filesystem paths to the browser.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate, TruncMonth
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from uploader.models import (
    AdPlacement,
    ApiClient,
    DownloadEvent,
    DownloadSession,
    ImportJob,
    SiteSetting,
    Video,
)
from uploader.services import storage_admin


def _actor(request) -> str:
    return getattr(request.user, "username", "") or ""


@require_GET
@staff_member_required
def dashboard(request):
    setting = SiteSetting.load()
    stats = storage_admin.stats()
    orphans = storage_admin.find_orphans()[:25]
    context = {
        "stats": stats,
        "orphans": orphans,
        "missing": storage_admin.find_missing_files()[:20],
        "partials": storage_admin.find_partials()[:20],
        "duplicates": storage_admin.duplicate_hashes()[:20],
        "recent_jobs": ImportJob.objects.select_related("video").order_by("-created_at")[:15],
        "ads": AdPlacement.objects.all(),
        "ads_enabled": setting.ads_enabled_globally,
        "api_clients": ApiClient.objects.all(),
        "setting": setting,
        "now": timezone.now(),
    }
    return render(request, "uploader/manage_dashboard.html", context)


@require_GET
@staff_member_required
def analytics(request):
    now = timezone.now()
    since = now - timedelta(days=30)
    sessions = DownloadSession.objects.all()
    completed = sessions.filter(status=DownloadSession.Status.COMPLETED)

    totals = {
        "sessions": sessions.count(),
        "completed": completed.count(),
        "premium_sessions": sessions.filter(is_premium=True).count(),
        "free_sessions": sessions.filter(is_premium=False).count(),
        "bytes_served": sessions.aggregate(total=Sum("bytes_served"))["total"] or 0,
        "range_requests": sessions.aggregate(total=Sum("range_requests"))["total"] or 0,
        "failed": sessions.filter(status=DownloadSession.Status.FAILED).count(),
    }
    conversion = (totals["completed"] / totals["sessions"] * 100) if totals["sessions"] else 0.0

    daily = (
        sessions.filter(created_at__gte=since)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(total=Count("id"), done=Count("id", filter=Q(status="completed")))
        .order_by("day")
    )
    monthly = (
        sessions.annotate(month=TruncMonth("created_at"))
        .values("month")
        .annotate(total=Count("id"))
        .order_by("-month")[:6]
    )
    top_videos = (
        Video.objects.annotate(
            download_count=Count("sessions", filter=Q(sessions__status="completed")),
            bytes_served=Sum("sessions__bytes_served"),
        )
        .filter(download_count__gt=0)
        .order_by("-download_count")[:10]
    )
    errors = (
        DownloadEvent.objects.filter(kind__in=[
            DownloadEvent.Kind.FAILED,
            DownloadEvent.Kind.TOKEN_EXPIRED,
            DownloadEvent.Kind.TOKEN_INVALID,
        ])
        .order_by("-created_at")[:20]
    )

    return render(
        request,
        "uploader/manage_analytics.html",
        {
            "totals": totals,
            "conversion": round(conversion, 1),
            "daily": list(daily),
            "monthly": list(monthly),
            "top_videos": top_videos,
            "errors": errors,
            "since": since,
            "now": now,
        },
    )


@require_POST
@staff_member_required
def delete_video(request, public_id: str):
    video = get_object_or_404(Video, public_id=public_id)
    storage_admin.delete_video(video, actor=_actor(request))
    return redirect("manage_dashboard")


@require_POST
@staff_member_required
def delete_orphan(request):
    relative = (request.POST.get("relative") or "").strip()
    if relative:
        try:
            storage_admin.delete_orphan(relative, actor=_actor(request))
        except ValueError:
            # Path traversal attempt: ignore silently, the dashboard is unchanged.
            pass
    return redirect("manage_dashboard")


@require_POST
@staff_member_required
def cleanup_partials(request):
    storage_admin.cleanup_partials()
    return redirect("manage_dashboard")


@require_POST
@staff_member_required
def toggle_ads(request):
    setting = SiteSetting.load()
    setting.ads_enabled_globally = not setting.ads_enabled_globally
    setting.save(update_fields=["ads_enabled_globally", "updated_at"])
    return redirect("manage_dashboard")


@require_POST
@staff_member_required
def retry_job(request, public_id: str):
    from uploader.services import queue

    job = get_object_or_404(ImportJob, public_id=public_id)
    try:
        queue.retry_job(job)
    except (queue.QueueError, queue.DuplicateImport):
        pass
    return redirect("manage_dashboard")
