"""URL routes for the storage / uploader service.

Only three surfaces exist today, and all three are deliberately small:

* ``/healthz`` ............ liveness for the hosting/monitoring layer
* ``/d/<id>/`` ............ public download page (added in Phase 5)
* ``/api/v1/...`` ......... HMAC-authenticated blog API (added in Phase 2/3)

The download gateway (``/dl/<token>/``) and the staff dashboard are mounted in
later phases; keeping the URLconf minimal now makes it obvious what is exposed.
"""

from django.contrib import admin
from django.urls import path

from uploader import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("admin/", admin.site.urls),
]
