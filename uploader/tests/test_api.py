"""Blog-facing API endpoint tests (brief sections 8, 15, 18)."""

from __future__ import annotations

import json
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from uploader.models import ApiClient, ImportJob
from uploader.services.api_auth import derive_key, signing_headers

SECRET = "api-test-shared-secret-0123456789abcdef"
KEY_ID = "blog-key"


@override_settings(SECURE_SSL_REDIRECT=False)
class ApiTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.client_record = ApiClient.objects.create(
            name="nzdworld-blog",
            key_id=KEY_ID,
            secret_hash=derive_key(SECRET),
            permissions=["import", "status"],
        )

    def _headers(self, method: str, path: str, body: bytes = b"", query: str = ""):
        return signing_headers(
            key_id=KEY_ID, key=derive_key(SECRET), method=method, path=path, query=query, body=body
        )

    def api_post(self, path: str, payload: dict | None = None, *, headers=None, raw: bytes | None = None):
        body = raw if raw is not None else json.dumps(payload or {}).encode()
        return self.client.post(
            path,
            data=body,
            content_type="application/json",
            **(headers if headers is not None else self._headers("POST", path, body)),
        )

    def api_get(self, path: str, *, headers=None):
        return self.client.get(path, **(headers or self._headers("GET", path)))


class AuthenticationTests(ApiTestBase):
    def test_unsigned_request_is_rejected(self):
        response = self.client.post(
            reverse("api:create_import"), data=b"{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "missing_key")

    def test_bad_signature_is_rejected(self):
        wrong = self._headers("POST", reverse("api:create_import"))
        wrong["HTTP_X_NZD_SIGNATURE"] = "0" * 64
        response = self.api_post(reverse("api:create_import"), {}, headers=wrong)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "bad_signature")

    def test_scope_is_enforced(self):
        ApiClient.objects.filter(key_id=KEY_ID).update(permissions=["import"])
        job = ImportJob.objects.create(source_url="https://example.com/a.mp4")
        response = self.api_get(reverse("api:import_status", args=[job.public_id]))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "forbidden_scope")

    def test_rate_limit_returns_429(self):
        with mock.patch("uploader.api.API_RATE_LIMIT", 1):
            first = self.api_post(reverse("api:create_import"), {"source_url": "https://e.com/1.mp4"})
            second = self.api_post(reverse("api:create_import"), {"source_url": "https://e.com/2.mp4"})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["error"], "rate_limited")


class CreateImportTests(ApiTestBase):
    def test_creates_a_queued_job(self):
        path = reverse("api:create_import")
        response = self.api_post(
            path, {"source_url": "https://example.com/Extraction-3-2026-nkiri.com.mkv", "title": "Extraction 3"}
        )
        self.assertEqual(response.status_code, 201)
        payload = response.json()["job"]
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["filename"], "")  # decided by the worker
        self.assertEqual(ImportJob.objects.count(), 1)

    def test_duplicate_source_returns_409_with_the_existing_job(self):
        path = reverse("api:create_import")
        first = self.api_post(path, {"source_url": "https://example.com/same.mp4"}).json()["job"]
        second = self.api_post(path, {"source_url": "https://example.com/same.mp4"})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["error"], "duplicate_import")
        self.assertEqual(second.json()["job"]["id"], first["id"])

    def test_rejects_missing_and_non_http_urls(self):
        path = reverse("api:create_import")
        self.assertEqual(self.api_post(path, {}).status_code, 400)
        self.assertEqual(
            self.api_post(path, {"source_url": "file:///etc/passwd"}).json()["error"],
            "invalid_source_url",
        )

    def test_manual_mode_requires_a_filename(self):
        path = reverse("api:create_import")
        response = self.api_post(
            path, {"source_url": "https://example.com/a.mp4", "filename_mode": "manual"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "missing_filename")

    def test_invalid_filename_mode_is_rejected(self):
        path = reverse("api:create_import")
        response = self.api_post(
            path, {"source_url": "https://example.com/a.mp4", "filename_mode": "wizard"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_filename_mode")

    def test_malformed_json_is_rejected(self):
        path = reverse("api:create_import")
        response = self.api_post(path, raw=b"{not json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "bad_json")


class StatusTests(ApiTestBase):
    def test_status_includes_video_and_logs(self):
        job = ImportJob.objects.create(
            source_url="https://example.com/a.mp4", title_hint="A", final_filename="a.mp4"
        )
        response = self.api_get(reverse("api:import_status", args=[job.public_id]))
        self.assertEqual(response.status_code, 200)
        payload = response.json()["job"]
        self.assertEqual(payload["id"], job.public_id)
        self.assertIn("logs", payload)
        self.assertIsNone(payload["video"])

    def test_unknown_id_is_404(self):
        response = self.api_get(reverse("api:import_status", args=["doesnotexist"]))
        self.assertEqual(response.status_code, 404)

    def test_get_only(self):
        job = ImportJob.objects.create(source_url="https://example.com/a.mp4")
        path = reverse("api:import_status", args=[job.public_id])
        response = self.api_post(path, {})
        self.assertEqual(response.status_code, 405)


class CancelRetryTests(ApiTestBase):
    def test_cancel_queued_job(self):
        job = ImportJob.objects.create(source_url="https://example.com/a.mp4")
        response = self.api_post(reverse("api:cancel_import", args=[job.public_id]), {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job"]["status"], "cancelled")
        job.refresh_from_db()
        self.assertIsNone(job.active_dedupe_key)

    def test_cancel_finished_job_is_409(self):
        job = ImportJob.objects.create(source_url="https://example.com/a.mp4")
        job.mark_terminal(ImportJob.Status.COMPLETED)
        response = self.api_post(reverse("api:cancel_import", args=[job.public_id]), {})
        self.assertEqual(response.status_code, 409)

    def test_retry_failed_job(self):
        job = ImportJob.objects.create(
            source_url="https://example.com/a.mp4", status=ImportJob.Status.FAILED
        )
        response = self.api_post(reverse("api:retry_import", args=[job.public_id]), {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job"]["status"], "queued")

    def test_retry_running_job_is_409(self):
        job = ImportJob.objects.create(
            source_url="https://example.com/a.mp4", status=ImportJob.Status.DOWNLOADING
        )
        response = self.api_post(reverse("api:retry_import", args=[job.public_id]), {})
        self.assertEqual(response.status_code, 409)


class FilenamePreviewTests(ApiTestBase):
    def test_preview_returns_the_branded_example(self):
        response = self.api_post(
            reverse("api:preview_filename"),
            {
                "original_filename": "Extraction-3-2026-nkiri.com.mkv",
                "title": "Extraction 3",
                "manual_name": "Extraction 3 2026.mkv",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["auto"], "extraction-3-2026 | NZDWorld.com.mkv")
        self.assertEqual(payload["manual"], "extraction-3-2026 | NZDWorld.com.mkv")
        self.assertEqual(payload["errors"], "")

    def test_preview_requires_an_original_filename(self):
        response = self.api_post(reverse("api:preview_filename"), {"title": "X"})
        self.assertEqual(response.status_code, 400)
