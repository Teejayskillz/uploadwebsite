"""URL routes for the storage / uploader service.

Public surface:

* ``/healthz`` ........... liveness
* ``/d/<id>/`` ........... download page
* ``/d/<id>/start/`` ..... mint a session + signed link
* ``/dl/<token>/`` ....... authorised byte delivery (Range supported)
* ``/t/<id>/`` ........... poster frame
* ``/api/v1/...`` ........ HMAC blog API
* ``/manage/...`` ........ staff dashboards (admin login required)
* ``/admin/`` ............ Django admin
"""

from django.contrib import admin
from django.urls import include, path

from uploader import views, views_download, views_manage

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    # Public download gateway
    path("d/<str:public_id>/", views_download.download_page, name="download_page"),
    path("d/<str:public_id>/start/", views_download.start_download, name="start_download"),
    path("dl/<str:token>/", views_download.download_file, name="download_file"),
    path("t/<str:public_id>/", views_download.serve_thumbnail, name="thumbnail"),
    # Staff dashboards
    path("manage/", views_manage.dashboard, name="manage_dashboard"),
    path("manage/analytics/", views_manage.analytics, name="manage_analytics"),
    path(
        "manage/videos/<str:public_id>/delete/",
        views_manage.delete_video,
        name="manage_delete_video",
    ),
    path("manage/orphans/delete/", views_manage.delete_orphan, name="manage_delete_orphan"),
    path(
        "manage/partials/cleanup/",
        views_manage.cleanup_partials,
        name="manage_cleanup_partials",
    ),
    path("manage/ads/toggle/", views_manage.toggle_ads, name="manage_toggle_ads"),
    path(
        "manage/jobs/<str:public_id>/retry/",
        views_manage.retry_job,
        name="manage_retry_job",
    ),
    # Machine API
    path("api/v1/", include("uploader.api_urls")),
    path("admin/", admin.site.urls),
]
