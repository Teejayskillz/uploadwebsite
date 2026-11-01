"""HMAC API authentication tests (brief sections 15 and 18)."""

from __future__ import annotations

import time

from django.test import RequestFactory, TestCase

from uploader.models import ApiClient
from uploader.services.api_auth import (
    ApiAuthError,
    authenticate,
    build_signature,
    derive_key,
    new_nonce,
    signing_headers,
)

SECRET = "a-very-long-shared-secret-for-tests-1234567890"
PATH = "/api/v1/imports/"


class ApiAuthTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.client_record = ApiClient.objects.create(
            name="nzdworld-blog",
            key_id="blog-key",
            secret_hash=derive_key(SECRET),
            permissions=["import", "status"],
        )

    def _request(self, *, body=b"{}", path=PATH, method="post", headers=None, query=""):
        headers = headers if headers is not None else signing_headers(
            key_id="blog-key", key=derive_key(SECRET), method=method.upper(), path=path, body=body
        )
        meta = dict(headers)
        url = path + (f"?{query}" if query else "")
        if method == "post":
            request = self.factory.post(url, data=body, content_type="application/json", **meta)
        else:
            request = self.factory.get(url, **meta)
        request.META["QUERY_STRING"] = query
        return request

    def test_valid_signature_authenticates(self):
        caller = authenticate(self._request(), required_scope="import")
        self.assertEqual(caller.client.key_id, "blog-key")
        self.assertEqual(caller.scope, "import")

    def test_missing_key_header_is_rejected(self):
        with self.assertRaises(ApiAuthError) as ctx:
            authenticate(self._request(headers={}))
        self.assertEqual(ctx.exception.code, "missing_key")

    def test_unknown_key_is_rejected(self):
        headers = signing_headers(
            key_id="nope", key=derive_key(SECRET), method="POST", path=PATH, body=b"{}"
        )
        with self.assertRaises(ApiAuthError) as ctx:
            authenticate(self._request(headers=headers))
        self.assertEqual(ctx.exception.code, "unknown_key")

    def test_inactive_key_is_rejected(self):
        self.client_record.is_active = False
        self.client_record.save(update_fields=["is_active"])
        with self.assertRaises(ApiAuthError) as ctx:
            authenticate(self._request())
        self.assertEqual(ctx.exception.code, "inactive_key")

    def test_tampered_body_is_rejected(self):
        headers = signing_headers(
            key_id="blog-key", key=derive_key(SECRET), method="POST", path=PATH, body=b'{"a":1}'
        )
        with self.assertRaises(ApiAuthError) as ctx:
            authenticate(self._request(body=b'{"a":2}', headers=headers))
        self.assertEqual(ctx.exception.code, "bad_signature")

    def test_wrong_path_is_rejected(self):
        headers = signing_headers(
            key_id="blog-key", key=derive_key(SECRET), method="POST", path="/api/v1/other/", body=b"{}"
        )
        with self.assertRaises(ApiAuthError) as ctx:
            authenticate(self._request(headers=headers))
        self.assertEqual(ctx.exception.code, "bad_signature")

    def test_wrong_secret_is_rejected(self):
        headers = signing_headers(
            key_id="blog-key", key=derive_key("wrong-secret"), method="POST", path=PATH, body=b"{}"
        )
        with self.assertRaises(ApiAuthError) as ctx:
            authenticate(self._request(headers=headers))
        self.assertEqual(ctx.exception.code, "bad_signature")

    def test_stale_timestamp_is_rejected(self):
        old = str(int(time.time()) - 3600)
        nonce = new_nonce()
        headers = {
            "HTTP_X_NZD_KEY": "blog-key",
            "HTTP_X_NZD_TIMESTAMP": old,
            "HTTP_X_NZD_NONCE": nonce,
            "HTTP_X_NZD_SIGNATURE": build_signature(
                key=derive_key(SECRET),
                method="POST",
                path=PATH,
                query="",
                body=b"{}",
                timestamp=old,
                nonce=nonce,
            ),
        }
        with self.assertRaises(ApiAuthError) as ctx:
            authenticate(self._request(headers=headers))
        self.assertEqual(ctx.exception.code, "stale_timestamp")

    def test_replayed_nonce_is_rejected(self):
        headers = signing_headers(
            key_id="blog-key", key=derive_key(SECRET), method="POST", path=PATH, body=b"{}"
        )
        authenticate(self._request(headers=dict(headers)))
        with self.assertRaises(ApiAuthError) as ctx:
            authenticate(self._request(headers=dict(headers)))
        self.assertEqual(ctx.exception.code, "replayed_nonce")

    def test_scope_is_enforced(self):
        with self.assertRaises(ApiAuthError) as ctx:
            authenticate(self._request(), required_scope="admin")
        self.assertEqual(ctx.exception.code, "forbidden_scope")

    def test_bad_timestamp_format_is_rejected(self):
        headers = signing_headers(
            key_id="blog-key", key=derive_key(SECRET), method="POST", path=PATH, body=b"{}"
        )
        headers["HTTP_X_NZD_TIMESTAMP"] = "not-a-number"
        with self.assertRaises(ApiAuthError) as ctx:
            authenticate(self._request(headers=headers))
        self.assertEqual(ctx.exception.code, "bad_timestamp")

    def test_successful_call_updates_usage_counters(self):
        authenticate(self._request())
        self.client_record.refresh_from_db()
        self.assertEqual(self.client_record.request_count, 1)
        self.assertIsNotNone(self.client_record.last_used_at)

    def test_query_string_is_part_of_the_signature(self):
        headers = signing_headers(
            key_id="blog-key",
            key=derive_key(SECRET),
            method="GET",
            path=PATH,
            query="page=1",
            body=b"",
        )
        # Signed with page=1 but sent with page=2 -> must fail.
        with self.assertRaises(ApiAuthError) as ctx:
            authenticate(
                self._request(method="get", body=b"", headers=headers, query="page=2")
            )
        self.assertEqual(ctx.exception.code, "bad_signature")
