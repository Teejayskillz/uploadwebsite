"""Streaming downloader tests: content sniffing, redirects, caps, cancellation."""

from __future__ import annotations

import hashlib
from pathlib import Path

from django.test import SimpleTestCase

from uploader.services.downloader import (
    DownloadError,
    DownloadResult,
    fetch_to_file,
    sniff_container,
)
from uploader.services.netguard import ValidatedURL
from uploader.tests.support import make_scratch_dir, remove_scratch_dir

MP4_HEAD = b"\x00\x00\x00\x18ftypmp42"
MKV_HEAD = b"\x1aE\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x1fB\x86\x81\x01B\xf7\x81\x01"
WEBM_HEAD = b"\x1aE\xdf\xa3\x9fB\x86\x81\x01B\xf7\x81\x01B\xf2\x81\x04B\xf3\x81\x08B\x82\x84webm"


def mp4_body(size: int = 2048) -> bytes:
    return MP4_HEAD + b"\x00" * max(0, size - len(MP4_HEAD))


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", chunk_size=None):
        self.status = status
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self._body = body
        self._pos = 0
        self._chunk = chunk_size or max(len(body), 1)
        self.closed = False

    def read(self, amount: int) -> bytes:
        size = min(amount, self._chunk)
        data = self._body[self._pos : self._pos + size]
        self._pos += len(data)
        return data

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[str] = []

    def __call__(self, validated: ValidatedURL):
        self.calls.append(validated.url)
        return self._responses.pop(0)


def target(url: str = "https://93.184.216.34/x.mp4") -> ValidatedURL:
    return ValidatedURL(
        url=url, scheme="https", host="93.184.216.34", port=443, addresses=("93.184.216.34",)
    )


class SniffTests(SimpleTestCase):
    def test_mp4(self):
        self.assertEqual(sniff_container(MP4_HEAD), "mp4")

    def test_quicktime(self):
        self.assertEqual(sniff_container(b"\x00\x00\x00\x14ftypqt  "), "mov")

    def test_mkv(self):
        self.assertEqual(sniff_container(MKV_HEAD), "mkv")

    def test_webm_is_distinguished_from_mkv(self):
        self.assertEqual(sniff_container(WEBM_HEAD), "webm")

    def test_avi(self):
        self.assertEqual(sniff_container(b"RIFF\x00\x00\x00\x00AVI "), "avi")

    def test_html_is_recognised(self):
        self.assertEqual(sniff_container(b"<!DOCTYPE html><html>nope"), "html")
        self.assertEqual(sniff_container(b"   <html><body>"), "html")

    def test_unknown(self):
        self.assertIsNone(sniff_container(b"just some text that is not a video"))
        self.assertIsNone(sniff_container(b""))


class DownloadTests(SimpleTestCase):
    def setUp(self):
        self._scratch = make_scratch_dir()
        self.addCleanup(remove_scratch_dir, self._scratch)
        self.dest = self._scratch / "video.part"

    def test_happy_path_writes_file_and_hashes_it(self):
        body = mp4_body()
        transport = FakeTransport(
            FakeResponse(200, {"content-type": "video/mp4", "content-length": str(len(body))}, body)
        )
        result = fetch_to_file(target(), self.dest, max_bytes=10_000, transport=transport)

        self.assertIsInstance(result, DownloadResult)
        self.assertEqual(result.container, "mp4")
        self.assertEqual(result.size_bytes, len(body))
        self.assertEqual(result.sha256, hashlib.sha256(body).hexdigest())
        self.assertTrue(self.dest.exists())
        self.assertEqual(self.dest.read_bytes(), body)

    def test_redirect_is_followed_and_counted(self):
        transport = FakeTransport(
            FakeResponse(302, {"location": "http://93.184.216.34/real.mp4"}),
            FakeResponse(200, {"content-type": "video/mp4"}, mp4_body()),
        )
        result = fetch_to_file(target(), self.dest, max_bytes=10_000, transport=transport)
        self.assertEqual(result.hops, 1)
        self.assertEqual(len(transport.calls), 2)

    def test_redirect_to_loopback_is_blocked(self):
        transport = FakeTransport(FakeResponse(301, {"location": "http://127.0.0.1/secret"}))
        with self.assertRaises(DownloadError) as ctx:
            fetch_to_file(target(), self.dest, max_bytes=10_000, transport=transport)
        self.assertEqual(ctx.exception.code, "unsafe_redirect")
        self.assertFalse(ctx.exception.retryable)
        self.assertFalse(self.dest.exists())

    def test_redirect_loop_is_capped(self):
        responses = [FakeResponse(302, {"location": "https://93.184.216.34/again"}) for _ in range(6)]
        with self.assertRaises(DownloadError) as ctx:
            fetch_to_file(target(), self.dest, max_bytes=10_000, transport=FakeTransport(*responses))
        self.assertEqual(ctx.exception.code, "too_many_redirects")

    def test_redirect_without_location_is_rejected(self):
        with self.assertRaises(DownloadError) as ctx:
            fetch_to_file(
                target(), self.dest, max_bytes=10_000, transport=FakeTransport(FakeResponse(302))
            )
        self.assertEqual(ctx.exception.code, "bad_redirect")

    def test_html_content_type_is_rejected(self):
        transport = FakeTransport(FakeResponse(200, {"content-type": "text/html"}, b"<html></html>"))
        with self.assertRaises(DownloadError) as ctx:
            fetch_to_file(target(), self.dest, max_bytes=10_000, transport=transport)
        self.assertEqual(ctx.exception.code, "html_response")
        self.assertFalse(self.dest.exists())

    def test_html_payload_disguised_as_video_is_rejected(self):
        """The brief's explicit case: an HTML error page served as video/mp4."""
        transport = FakeTransport(
            FakeResponse(200, {"content-type": "video/mp4"}, b"<!DOCTYPE html><html>error</html>")
        )
        with self.assertRaises(DownloadError) as ctx:
            fetch_to_file(target(), self.dest, max_bytes=10_000, transport=transport)
        self.assertEqual(ctx.exception.code, "html_response")
        self.assertFalse(self.dest.exists())

    def test_unknown_container_is_rejected_and_file_cleaned(self):
        transport = FakeTransport(
            FakeResponse(200, {"content-type": "application/octet-stream"}, b"not a video at all")
        )
        with self.assertRaises(DownloadError) as ctx:
            fetch_to_file(target(), self.dest, max_bytes=10_000, transport=transport)
        self.assertEqual(ctx.exception.code, "unknown_container")
        self.assertFalse(self.dest.exists())

    def test_declared_size_over_limit_is_refused_before_transfer(self):
        transport = FakeTransport(
            FakeResponse(200, {"content-type": "video/mp4", "content-length": "999999999"}, mp4_body())
        )
        with self.assertRaises(DownloadError) as ctx:
            fetch_to_file(target(), self.dest, max_bytes=1024, transport=transport)
        self.assertEqual(ctx.exception.code, "file_too_large")
        self.assertFalse(ctx.exception.retryable)

    def test_streamed_size_over_limit_is_refused(self):
        body = mp4_body(4096)
        transport = FakeTransport(
            FakeResponse(200, {"content-type": "video/mp4"}, body, chunk_size=256)
        )
        with self.assertRaises(DownloadError) as ctx:
            fetch_to_file(target(), self.dest, max_bytes=1024, transport=transport)
        self.assertEqual(ctx.exception.code, "file_too_large")
        self.assertFalse(self.dest.exists())

    def test_cancellation_stops_the_transfer(self):
        body = mp4_body(8192)
        transport = FakeTransport(
            FakeResponse(200, {"content-type": "video/mp4"}, body, chunk_size=128)
        )
        calls = {"n": 0}

        def should_cancel() -> bool:
            calls["n"] += 1
            return calls["n"] > 2

        with self.assertRaises(DownloadError) as ctx:
            fetch_to_file(
                target(),
                self.dest,
                max_bytes=1_000_000,
                transport=transport,
                should_cancel=should_cancel,
            )
        self.assertEqual(ctx.exception.code, "cancelled")
        self.assertFalse(self.dest.exists())

    def test_progress_callback_is_called(self):
        body = mp4_body(3 * 1024 * 1024)
        seen: list[tuple[int, int | None]] = []
        transport = FakeTransport(FakeResponse(200, {"content-type": "video/mp4"}, body))
        fetch_to_file(
            target(),
            self.dest,
            max_bytes=10 * 1024 * 1024,
            transport=transport,
            on_progress=lambda done, total: seen.append((done, total)),
        )
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], len(body))

    def test_http_error_codes_are_classified(self):
        for status, retryable in ((404, False), (429, True), (500, True)):
            with self.subTest(status=status):
                transport = FakeTransport(FakeResponse(status))
                with self.assertRaises(DownloadError) as ctx:
                    fetch_to_file(target(), self.dest, max_bytes=10_000, transport=transport)
                self.assertEqual(ctx.exception.code, f"http_{status}")
                self.assertEqual(ctx.exception.retryable, retryable)

    def test_empty_body_is_rejected(self):
        transport = FakeTransport(FakeResponse(200, {"content-type": "video/mp4"}, b""))
        with self.assertRaises(DownloadError) as ctx:
            fetch_to_file(target(), self.dest, max_bytes=10_000, transport=transport)
        self.assertEqual(ctx.exception.code, "empty_response")
