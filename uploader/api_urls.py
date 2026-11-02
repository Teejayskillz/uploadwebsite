"""URL map for the machine API. Mounted under ``/api/v1/`` by ``config.urls``."""

from django.urls import path

from uploader import api

app_name = "api"

urlpatterns = [
    path("imports/", api.create_import, name="create_import"),
    path("imports/<str:public_id>/", api.import_status, name="import_status"),
    path("imports/<str:public_id>/cancel/", api.cancel_import, name="cancel_import"),
    path("imports/<str:public_id>/retry/", api.retry_import, name="retry_import"),
    path("filenames/preview/", api.preview_filename, name="preview_filename"),
    path("urls/verify/", api.verify_url, name="verify_url"),
]
