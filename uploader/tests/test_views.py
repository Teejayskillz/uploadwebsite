"""Public health endpoint tests."""

from __future__ import annotations

from pathlib import Path

from django.test import SimpleTestCase, override_settings

from uploader.tests.support import make_scratch_dir, remove_scratch_dir


@override_settings(SECURE_SSL_REDIRECT=False)
class HealthzTests(SimpleTestCase):
    def setUp(self):
        self.scratch = make_scratch_dir()
        self.addCleanup(remove_scratch_dir, self.scratch)

    def test_reports_ready_state_and_counts(self):
        with override_settings(VIDEO_ROOT=str(self.scratch / "videos")):
            (self.scratch / "videos").mkdir(exist_ok=True)
            response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["storage_ready"])
        self.assertIsInstance(payload["free_bytes"], int)
        self.assertGreater(payload["free_bytes"], 0)
        self.assertEqual(payload["service"], "nzdworld-uploader")
        self.assertIn("media_tooling", payload)

    def test_missing_video_root_is_reported_not_hidden_as_zero(self):
        missing = self.scratch / "does-not-exist"
        with override_settings(VIDEO_ROOT=str(missing)):
            response = self.client.get("/healthz")
        payload = response.json()
        self.assertFalse(payload["storage_ready"])
        self.assertIsNone(payload["free_bytes"])
        self.assertFalse(payload["ok"])

    def test_does_not_leak_absolute_paths(self):
        with override_settings(VIDEO_ROOT=str(self.scratch)):
            body = self.client.get("/healthz").content.decode()
        self.assertNotIn(str(Path.home()), body)
        self.assertNotIn("jaraflix", body)
