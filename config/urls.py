"""URL routes for the storage / uploader service.

Public surface is deliberately tiny:

* ``/healthz`` ............ liveness for the hosting/monitoring layer
* ``/api/v1/...`` ......... HMAC-authenticated blog API
* ``/admin/`` ............. staff-only administration

The download gateway (``/d/<public_id>/`` and ``/dl/<token>/``) arrives in
Phase 5; keeping this file minimal makes the exposed surface obvious.
"""

from django.contrib import admin
from django.urls import include, path

from uploader import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("api/v1/", include("uploader.api_urls")),
    path("admin/", admin.site.urls),
]
