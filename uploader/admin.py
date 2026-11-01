"""Staff-only administration for the storage service.

Everything here is gated by ``is_staff`` (Django admin). The public download
gateway never touches these models directly through admin URLs.
"""

from django.contrib import admin

from .models import (
    AdPlacement,
    ApiClient,
    ApiNonce,
    AuditLog,
    BrandRule,
    DownloadEvent,
    DownloadSession,
    ImportJob,
    JobLog,
    PremiumAssertion,
    SiteSetting,
    Video,
)


@admin.register(SiteSetting)
class SiteSettingAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        # Singleton: the row is created by SiteSetting.load().
        return not SiteSetting.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BrandRule)
class BrandRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "pattern", "is_active", "display_order")
    list_filter = ("kind", "is_active")
    list_editable = ("is_active", "display_order")
    search_fields = ("name", "pattern")


@admin.register(AdPlacement)
class AdPlacementAdmin(admin.ModelAdmin):
    list_display = ("name", "slot", "is_active", "display_order")
    list_filter = ("slot", "is_active")
    list_editable = ("is_active", "display_order")


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    list_display = (
        "stored_filename",
        "status",
        "human_size",
        "duration_seconds",
        "metadata_status",
        "created_at",
    )
    list_filter = ("status", "metadata_status", "extension")
    search_fields = ("stored_filename", "title", "original_filename", "sha256")
    readonly_fields = (
        "public_id",
        "relative_path",
        "sha256",
        "size_bytes",
        "probe",
        "streams",
        "created_at",
        "updated_at",
    )
    date_hierarchy = "created_at"

    @admin.display(description="Size")
    def human_size(self, obj: Video) -> str:
        size = float(obj.size_bytes or 0)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{obj.size_bytes} B"


class JobLogInline(admin.TabularInline):
    model = JobLog
    extra = 0
    can_delete = False
    readonly_fields = ("level", "message", "created_at")


@admin.register(ImportJob)
class ImportJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "status",
        "progress_percent",
        "final_filename",
        "created_by",
        "attempts",
        "created_at",
    )
    list_filter = ("status", "url_type", "filename_mode")
    search_fields = ("source_url", "final_filename", "title_hint")
    readonly_fields = (
        "public_id",
        "active_dedupe_key",
        "dedupe_key",
        "bytes_downloaded",
        "worker_pid",
        "locked_at",
        "heartbeat_at",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    )
    inlines = [JobLogInline]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        # Jobs are created through the API/queue, never by hand.
        return False


@admin.register(DownloadSession)
class DownloadSessionAdmin(admin.ModelAdmin):
    list_display = (
        "public_id",
        "video",
        "is_premium",
        "status",
        "range_requests",
        "bytes_served",
        "created_at",
    )
    list_filter = ("status", "is_premium")
    search_fields = ("public_id", "video__stored_filename", "premium_subject")
    readonly_fields = ("public_id", "created_at", "visitor_hash")


@admin.register(DownloadEvent)
class DownloadEventAdmin(admin.ModelAdmin):
    list_display = ("kind", "video", "bytes_count", "is_range_request", "created_at")
    list_filter = ("kind", "is_range_request")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False


@admin.register(ApiClient)
class ApiClientAdmin(admin.ModelAdmin):
    list_display = ("name", "key_id", "is_active", "last_used_at", "request_count")
    list_filter = ("is_active",)
    readonly_fields = ("secret_hash", "last_used_at", "request_count")


@admin.register(ApiNonce)
class ApiNonceAdmin(admin.ModelAdmin):
    list_display = ("key_id", "nonce", "created_at", "expires_at")
    readonly_fields = ("key_id", "nonce", "created_at", "expires_at")


@admin.register(PremiumAssertion)
class PremiumAssertionAdmin(admin.ModelAdmin):
    list_display = ("subject_id", "is_premium", "expires_at", "verified_at")
    list_filter = ("is_premium",)
    readonly_fields = ("verified_at",)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("action", "target_type", "target_id", "actor", "created_at")
    list_filter = ("action",)
    readonly_fields = ("actor", "action", "target_type", "target_id", "detail", "created_at")

    def has_add_permission(self, request):
        return False
