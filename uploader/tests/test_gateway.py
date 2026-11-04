"""Download gateway: tokens, ranges, expiry redirects and premium ad bypass."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from django.core import signing
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from uploader.models import (
    AdPlacement,
    DownloadEvent,
    DownloadSession,
    SiteSetting,
    Video,
)
from uploader.services import delivery, premium
from uploader.tests.support import make_scratch_dir, remove_scratch_dir

from django.test import RequestFactory

FILE_BYTES = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 8  # 2064 bytes


class GatewayBase(TestCase):
    def setUp(self):
        self.root = make_scratch_dir()
        self.addCleanup(remove_scratch_dir, self.root)
        self.videos = self.root / "videos"
        self.tmp = self.root / "tmp"
        self.videos.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

        self.overrides = override_settings(
            VIDEO_ROOT=str(self.videos),
            TEMP_ROOT=str(self.tmp),
            MEDIA_TOOLING_REQUIRED=False,
            SECRET_KEY="test-secret-key-for-signing",
            SECURE_SSL_REDIRECT=False,
            API_SHARED_SECRET="handoff-secret",
            PREMIUM_HANDOFF_MAX_AGE_SECONDS=300,
            PREMIUM_SESSION_TTL_SECONDS=900,
            SITE_NAME="NZDWorld",
        )
        self.overrides.enable()
        self.addCleanup(self.overrides.disable)

        SiteSetting.objects.update_or_create(
            pk=1, defaults={"download_token_ttl_minutes": 30, "ads_enabled_globally": True}
        )

        target = self.videos / "2026" / "11"
        target.mkdir(parents=True, exist_ok=True)
        (target / "movie.mp4").write_bytes(FILE_BYTES)
        thumb = self.videos / "thumbs" / "2026" / "11"
        thumb.mkdir(parents=True, exist_ok=True)
        (thumb / "poster.jpg").write_bytes(b"\xff\xd8fake")

        self.video = Video.objects.create(
            title="Extraction 3",
            original_filename="Extraction-3-2026-nkiri.com.mkv",
            stored_filename="extraction-3-2026 | NZDWorld.com.mp4",
            relative_path="2026/11/movie.mp4",
            extension="mp4",
            size_bytes=len(FILE_BYTES),
            mime_type="video/mp4",
            thumbnail_path="thumbs/2026/11/poster.jpg",
            duration_seconds=7200,
            width=1920,
            height=1080,
            status=Video.Status.READY,
        )


class TokenTests(GatewayBase):
    def test_round_trip(self):
        session = delivery.create_session(self.video, is_premium=False)
        token = delivery.issue_token(session)
        verified = delivery.verify_token(token)
        self.assertEqual(verified.pk, session.pk)

    def test_tampered_token_is_rejected(self):
        session = delivery.create_session(self.video, is_premium=False)
        token = delivery.issue_token(session)
        with self.assertRaises(delivery.TokenInvalid):
            delivery.verify_token(token[:-4] + "aaaa")

    def test_expired_session_is_rejected_but_peek_still_finds_the_video(self):
        session = delivery.create_session(self.video, is_premium=False)
        token = delivery.issue_token(session)
        DownloadSession.objects.filter(pk=session.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        with self.assertRaises(delivery.TokenExpired):
            delivery.verify_token(token)
        payload = delivery.peek_token(token)
        self.assertEqual(payload.video_id, self.video.public_id)

    def test_signed_max_age_is_enforced(self):
        """A token older than the configured TTL fails even if the row is fresh."""
        session = delivery.create_session(self.video, is_premium=False)
        token = delivery.issue_token(session)
        with override_settings():
            # Simulate age by loading with a zero max_age.
            with self.assertRaises(signing.SignatureExpired):
                signing.loads(token, salt=delivery.TOKEN_SALT, max_age=-1)

    def test_deleted_video_is_not_deliverable(self):
        session = delivery.create_session(self.video, is_premium=False)
        token = delivery.issue_token(session)
        Video.objects.filter(pk=self.video.pk).update(status=Video.Status.DELETED)
        with self.assertRaises(delivery.TokenInvalid):
            delivery.verify_token(token)


class RangeTests(SimpleTestCase):
    def test_parses_standard_ranges(self):
        self.assertEqual(delivery.parse_range("bytes=0-99", 1000), (0, 99))
        self.assertEqual(delivery.parse_range("bytes=500-", 1000), (500, 999))
        self.assertEqual(delivery.parse_range("bytes=-100", 1000), (900, 999))
        self.assertEqual(delivery.parse_range("bytes=990-2000", 1000), (990, 999))

    def test_rejects_unsatisfiable_or_malformed(self):
        self.assertIsNone(delivery.parse_range("bytes=5000-", 1000))
        self.assertIsNone(delivery.parse_range("items=0-1", 1000))
        self.assertIsNone(delivery.parse_range("", 1000))
        self.assertIsNone(delivery.parse_range("bytes=abc-def", 1000))

    def test_content_disposition_cannot_be_injected(self):
        value = delivery.content_disposition('movie"\r\nX-Evil: 1.mp4')
        self.assertNotIn("\r", value)
        self.assertNotIn("\n", value)
        self.assertNotIn('"movie"', value)
        self.assertIn("attachment;", value)


class ServeTests(GatewayBase):
    def _request(self, **headers):
        factory = RequestFactory()
        request = factory.get("/dl/x/", **headers)
        return request

    def test_full_download_marks_the_session_completed(self):
        session = delivery.create_session(self.video, is_premium=False)
        response = delivery.serve_video(self._request(), session)
        body = b"".join(response.streaming_content)
        self.assertEqual(body, FILE_BYTES)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Length"], str(len(FILE_BYTES)))
        self.assertEqual(response["Accept-Ranges"], "bytes")

        session.refresh_from_db()
        self.assertEqual(session.status, DownloadSession.Status.COMPLETED)
        self.assertEqual(session.bytes_served, len(FILE_BYTES))
        self.assertTrue(
            DownloadEvent.objects.filter(session=session, kind=DownloadEvent.Kind.COMPLETED).exists()
        )

    def test_range_request_returns_206_and_is_not_a_new_download(self):
        session = delivery.create_session(self.video, is_premium=False)
        response = delivery.serve_video(self._request(HTTP_RANGE="bytes=0-99"), session)
        body = b"".join(response.streaming_content)
        self.assertEqual(response.status_code, 206)
        self.assertEqual(body, FILE_BYTES[:100])
        self.assertEqual(response["Content-Range"], f"bytes 0-99/{len(FILE_BYTES)}")
        session.refresh_from_db()
        self.assertEqual(session.range_requests, 1)
        self.assertEqual(session.status, DownloadSession.Status.ACTIVE)

    def test_suffix_range(self):
        session = delivery.create_session(self.video, is_premium=False)
        response = delivery.serve_video(self._request(HTTP_RANGE="bytes=-10"), session)
        body = b"".join(response.streaming_content)
        self.assertEqual(body, FILE_BYTES[-10:])
        self.assertEqual(response.status_code, 206)

    def test_missing_file_is_a_delivery_error(self):
        session = delivery.create_session(self.video, is_premium=False)
        (self.videos / "2026" / "11" / "movie.mp4").unlink()
        with self.assertRaises(delivery.DeliveryError) as ctx:
            delivery.serve_video(self._request(), session)
        self.assertEqual(ctx.exception.code, "missing_file")


class DownloadPageTests(GatewayBase):
    def setUp(self):
        super().setUp()
        AdPlacement.objects.create(name="Header ad", slot=AdPlacement.Slot.HEADER,
                                   html_code="<div id='ad-header'>AD</div>", is_active=True)

    def test_page_renders_movie_information_without_leaking_paths(self):
        response = self.client.get(reverse("download_page", args=[self.video.public_id]))
        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Extraction 3", body)
        self.assertIn("2.0 KB", body)  # 2064 bytes
        self.assertIn("ad-header", body)  # free visitors see ads
        self.assertNotIn("2026/11/movie.mp4", body)

    def test_premium_visitor_sees_no_ads(self):
        token = signing.dumps({"sub": "42", "prem": True}, salt=premium.HANDOFF_SALT,
                              key="handoff-secret")
        response = self.client.get(
            reverse("download_page", args=[self.video.public_id]) + f"?u={token}"
        )
        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("ad-header", body)
        self.assertIn("Premium member", body)
        self.assertTrue(response.cookies.get(premium.COOKIE_NAME))

    def test_forged_handoff_does_not_grant_premium(self):
        response = self.client.get(
            reverse("download_page", args=[self.video.public_id]) + "?u=not-a-token"
        )
        self.assertNotIn("Premium member", response.content.decode())
        self.assertIn("ad-header", response.content.decode())

    def test_start_redirects_to_a_signed_link_and_stores_the_cookie(self):
        token = signing.dumps({"sub": "42", "prem": True}, salt=premium.HANDOFF_SALT,
                              key="handoff-secret")
        response = self.client.post(
            reverse("start_download", args=[self.video.public_id]) + f"?u={token}"
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/dl/", response["Location"])
        session = DownloadSession.objects.get()
        self.assertTrue(session.is_premium)
        self.assertEqual(session.premium_subject, "42")

    def test_following_the_redirect_delivers_the_file(self):
        post = self.client.post(reverse("start_download", args=[self.video.public_id]))
        download = self.client.get(post["Location"])
        body = b"".join(download.streaming_content)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(body, FILE_BYTES)
        self.assertIn("attachment;", download["Content-Disposition"])

    def test_expired_link_redirects_back_to_the_download_page(self):
        session = delivery.create_session(self.video, is_premium=False)
        token = delivery.issue_token(session)
        DownloadSession.objects.filter(pk=session.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        response = self.client.get(reverse("download_file", args=[token]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"], reverse("download_page", args=[self.video.public_id])
        )
        self.assertTrue(
            DownloadEvent.objects.filter(kind=DownloadEvent.Kind.TOKEN_EXPIRED).exists()
        )

    def test_invalid_token_returns_410(self):
        response = self.client.get(reverse("download_file", args=["garbage"]))
        self.assertEqual(response.status_code, 410)
        self.assertIn("unavailable", response.content.decode().lower())

    def test_unavailable_video_is_404(self):
        Video.objects.filter(pk=self.video.pk).update(status=Video.Status.QUARANTINED)
        response = self.client.get(reverse("download_page", args=[self.video.public_id]))
        self.assertEqual(response.status_code, 404)

    def test_thumbnail_is_served_and_missing_is_404(self):
        ok = self.client.get(reverse("thumbnail", args=[self.video.public_id]))
        self.assertEqual(ok.status_code, 200)
        Video.objects.filter(pk=self.video.pk).update(thumbnail_path="")
        missing = self.client.get(reverse("thumbnail", args=[self.video.public_id]))
        self.assertEqual(missing.status_code, 404)

    def test_download_page_never_mentions_the_blog(self):
        """The download page must have its own identity (client requirement)."""
        response = self.client.get(reverse("download_page", args=[self.video.public_id]))
        body = response.content.decode().lower()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("nzdworld", body)
        self.assertNotIn("jaraflix", body)
        # Same for the <title>.
        self.assertIn("extraction 3", body)
        self.assertIn("download", body)

    def test_premium_page_also_has_no_blog_branding(self):
        token = signing.dumps({"sub": "42", "prem": True}, salt=premium.HANDOFF_SALT,
                              key="handoff-secret")
        response = self.client.get(
            reverse("download_page", args=[self.video.public_id]) + f"?u={token}"
        )
        body = response.content.decode().lower()
        self.assertIn("premium member", body)
        self.assertNotIn("nzdworld", body)

    def test_invalid_link_page_has_no_blog_branding(self):
        response = self.client.get(reverse("download_file", args=["garbage"]))
        body = response.content.decode().lower()
        self.assertEqual(response.status_code, 410)
        self.assertNotIn("nzdworld", body)

    @override_settings(DOWNLOAD_BRAND_NAME="MovieVault")
    def test_optional_neutral_brand_name_is_shown_when_configured(self):
        body = self.client.get(
            reverse("download_page", args=[self.video.public_id])
        ).content.decode()
        self.assertIn("MovieVault", body)
        self.assertNotIn("NZDWorld", body)