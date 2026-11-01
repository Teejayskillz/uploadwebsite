"""Staff dashboard tests: access control, deletion, orphan cleanup, analytics."""

from __future__ import annotations

from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from uploader.models import DownloadSession, SiteSetting, Video
from uploader.tests.support import make_scratch_dir, remove_scratch_dir


@override_settings(SECURE_SSL_REDIRECT=False)
class ManageBase(TestCase):
    def setUp(self):
        self.root = make_scratch_dir()
        self.addCleanup(remove_scratch_dir, self.root)
        self.videos = self.root / "videos"
        self.tmp = self.root / "tmp"
        (self.videos / "2026" / "11").mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.overrides = override_settings(
            VIDEO_ROOT=str(self.videos),
            TEMP_ROOT=str(self.tmp),
            MEDIA_TOOLING_REQUIRED=False,
            API_SHARED_SECRET="x",
        )
        self.overrides.enable()
        self.addCleanup(self.overrides.disable)
        SiteSetting.objects.update_or_create(pk=1, defaults={"ads_enabled_globally": True})

        (self.videos / "2026" / "11" / "movie.mp4").write_bytes(b"x" * 100)
        (self.videos / "orphan.bin").write_bytes(b"y" * 40)
        (self.tmp / "leftover.part").write_bytes(b"z" * 10)

        self.video = Video.objects.create(
            title="Movie",
            original_filename="movie.mp4",
            stored_filename="movie | NZDWorld.com.mp4",
            relative_path="2026/11/movie.mp4",
            extension="mp4",
            size_bytes=100,
            status=Video.Status.READY,
        )

        self.staff = User.objects.create_user("staff", "s@example.com", "pw", is_staff=True)
        self.plain = User.objects.create_user("plain", "p@example.com", "pw")

    def as_staff(self):
        self.client.force_login(self.staff)


class AccessTests(ManageBase):
    def test_anonymous_is_redirected_to_admin_login(self):
        response = self.client.get(reverse("manage_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_non_staff_is_refused(self):
        self.client.force_login(self.plain)
        response = self.client.get(reverse("manage_dashboard"))
        self.assertEqual(response.status_code, 302)

    def test_staff_sees_the_dashboard(self):
        self.as_staff()
        response = self.client.get(reverse("manage_dashboard"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Storage &amp; queue", body)
        self.assertIn("movie | NZDWorld.com.mp4", body)
        self.assertIn("orphan.bin", body)
        # Absolute paths must never reach the browser.
        self.assertNotIn(str(self.videos), body)

    def test_staff_sees_analytics(self):
        self.as_staff()
        response = self.client.get(reverse("manage_analytics"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Download analytics", response.content.decode())


class DeletionTests(ManageBase):
    def test_deleting_a_video_removes_the_file_and_marks_the_row(self):
        self.as_staff()
        response = self.client.post(
            reverse("manage_delete_video", args=[self.video.public_id]), follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse((self.videos / "2026" / "11" / "movie.mp4").exists())
        self.video.refresh_from_db()
        self.assertEqual(self.video.status, Video.Status.DELETED)
        self.assertIsNotNone(self.video.deleted_at)
        # Row is kept for history/analytics.
        self.assertTrue(Video.objects.filter(pk=self.video.pk).exists())

    def test_orphan_delete_only_touches_unreferenced_files(self):
        self.as_staff()
        self.client.post(reverse("manage_delete_orphan"), {"relative": "orphan.bin"})
        self.assertFalse((self.videos / "orphan.bin").exists())

        self.client.post(
            reverse("manage_delete_orphan"), {"relative": "2026/11/movie.mp4"}
        )
        self.assertTrue((self.videos / "2026" / "11" / "movie.mp4").exists())

    def test_orphan_delete_cannot_escape_the_root(self):
        self.as_staff()
        self.client.post(reverse("manage_delete_orphan"), {"relative": "../../../etc/passwd"})
        # Nothing outside the root is ever touched.
        self.assertTrue((self.videos / "2026" / "11" / "movie.mp4").exists())

    def test_partial_cleanup(self):
        self.as_staff()
        self.client.post(reverse("manage_cleanup_partials"))
        self.assertFalse((self.tmp / "leftover.part").exists())

    def test_ads_toggle(self):
        self.as_staff()
        self.assertTrue(SiteSetting.load().ads_enabled_globally)
        self.client.post(reverse("manage_toggle_ads"))
        self.assertFalse(SiteSetting.load().ads_enabled_globally)


class AnalyticsDataTests(ManageBase):
    def test_completed_sessions_and_bandwidth_are_reported(self):
        DownloadSession.objects.create(
            video=self.video,
            is_premium=True,
            expires_at=self.video.created_at,
            status=DownloadSession.Status.COMPLETED,
            bytes_served=5000,
            range_requests=2,
        )
        self.as_staff()
        response = self.client.get(reverse("manage_analytics"))
        body = response.content.decode()
        self.assertIn("4.9", body)  # 5000 bytes, filesizeformat uses a narrow nbsp
        self.assertIn("100.0%", body)  # completion rate
