"""URL verification: HEAD, ranged-GET fallback, HTML detection, SSRF refusal."""

from __future__ import annotations

import json
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from uploader.models import ApiClient
from uploader.services import urlcheck
from uploader.services.api_auth import derive_key, signing_headers

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1024
SECRET = "verify-test-secret-0123456789"


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b""):
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._body = body
        self.closed = False

    def read(self, amount):
        return self._body[:amount]

    def close(self):
        self.closed = True


PUBLIC = "https://93.184.216.34/movie.mp4"


class VerifyTests(SimpleTestCase):
    def _opener(self, responses):
        calls = []

        def opener(validated, method, **kwargs):
            calls.append((validated.url, method, kwargs.get("extra_headers")))
            return responses.pop(0)

        opener.calls = calls
        return opener

    def test_head_with_video_type_is_valid(self):
        opener = self._opener([FakeResponse(200, {"content-type": "video/x-matroska", "content-length": "1048576"})])
        result = urlcheck.verify_url(PUBLIC, opener=opener)
        self.assertEqual(result.status, "valid")
        self.assertEqual(result.method, "HEAD")
        self.assertEqual(result.content_length, 1048576)
        self.assertEqual(result.filename, "movie.mp4")

    def test_html_is_invalid_even_with_200(self):
        opener = self._opener([FakeResponse(200, {"content-type": "text/html; charset=utf-8"})])
        result = urlcheck.verify_url(PUBLIC, opener=opener)
        self.assertEqual(result.status, "invalid")
        self.assertIn("web page", result.reason)

    def test_head_not_allowed_falls_back_to_ranged_get(self):
        opener = self._opener([
            FakeResponse(405),
            FakeResponse(206, {"content-type": "application/octet-stream"}, MP4),
        ])
        result = urlcheck.verify_url(PUBLIC, opener=opener)
        self.assertEqual(result.status, "valid")
        self.assertEqual(result.method, "GET")
        # The fallback must ask for a small range, not the whole file.
        self.assertEqual(opener.calls[1][2], {"Range": "bytes=0-2047"})

    def test_ranged_get_html_is_invalid(self):
        opener = self._opener([FakeResponse(403), FakeResponse(200, {}, b"<!DOCTYPE html><html>no")])
        result = urlcheck.verify_url(PUBLIC, opener=opener)
        self.assertEqual(result.status, "invalid")

    def test_unknown_bytes_are_inconclusive_not_valid(self):
        opener = self._opener([FakeResponse(405), FakeResponse(206, {}, b"random-bytes-here")])
        result = urlcheck.verify_url(PUBLIC, opener=opener)
        self.assertEqual(result.status, "inconclusive")

    def test_missing_file_is_invalid(self):
        opener = self._opener([FakeResponse(404)])
        result = urlcheck.verify_url(PUBLIC, opener=opener)
        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.http_status, 404)

    def test_unknown_content_type_is_inconclusive(self):
        opener = self._opener([FakeResponse(200, {"content-length": "500"})])
        result = urlcheck.verify_url(PUBLIC, opener=opener)
        self.assertEqual(result.status, "inconclusive")

    def test_blocked_url_never_opens_a_connection(self):
        opener = self._opener([])
        result = urlcheck.verify_url("http://127.0.0.1/secret.mp4", opener=opener)
        self.assertEqual(result.status, "invalid")
        self.assertEqual(opener.calls, [])

    def test_redirect_is_followed_and_revalidated(self):
        opener = self._opener([
            FakeResponse(302, {"location": "https://93.184.216.34/final.mp4"}),
            FakeResponse(200, {"content-type": "video/mp4", "content-length": "10"}),
        ])
        result = urlcheck.verify_url(PUBLIC, opener=opener)
        self.assertEqual(result.status, "valid")
        self.assertTrue(result.final_url.endswith("/final.mp4"))

    def test_redirect_to_private_address_is_rejected(self):
        opener = self._opener([FakeResponse(301, {"location": "http://127.0.0.1/x"})])
        result = urlcheck.verify_url(PUBLIC, opener=opener)
        self.assertEqual(result.status, "invalid")
        self.assertIn("Redirect blocked", result.reason)

    def test_unreachable_host_is_inconclusive(self):
        from uploader.services.downloader import DownloadError

        def boom(validated, method, **kwargs):
            raise DownloadError("connect_failed", "refused")

        result = urlcheck.verify_url(PUBLIC, opener=boom)
        self.assertEqual(result.status, "inconclusive")


@override_settings(SECURE_SSL_REDIRECT=False)
class VerifyEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        ApiClient.objects.create(
            name="blog", key_id="k", secret_hash=derive_key(SECRET), permissions=["import"]
        )

    def _post(self, payload):
        body = json.dumps(payload).encode()
        return self.client.post(
            reverse("api:verify_url"),
            data=body,
            content_type="application/json",
            **signing_headers(key_id="k", key=derive_key(SECRET), method="POST", path=reverse("api:verify_url"), body=body),
        )

    def test_requires_authentication(self):
        response = self.client.post(reverse("api:verify_url"), data=b"{}", content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_returns_the_check_result(self):
        with mock.patch(
            "uploader.services.urlcheck.verify_url",
            return_value=urlcheck.UrlCheck(status="valid", reason="ok", http_status=200),
        ):
            response = self._post({"url": PUBLIC})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "valid")

    def test_missing_url_is_rejected(self):
        response = self._post({})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "missing_url")
