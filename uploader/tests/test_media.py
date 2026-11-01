"""
FFmpeg integration tests.

Two layers:

* **Unit tests** (always run): argv construction, JSON parsing, error mapping,
  stream comparison, timeout handling. They use a fake ``subprocess.run`` so no
  binary is required.
* **Integration tests** (skipped when FFmpeg is absent): real probe/stream-copy/
  thumbnail on a generated clip. These are the ones that must pass on the server;
  locally they skip rather than pretend.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock, skipUnless

from django.test import SimpleTestCase, override_settings

from uploader.services import media
from uploader.tests.support import make_scratch_dir, remove_scratch_dir

PROBE_JSON = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "24000/1001",
            "duration": "120.500000",
        },
        {"codec_type": "audio", "codec_name": "aac", "channels": 2, "tags": {"language": "eng"}},
        {"codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
    ],
    "format": {
        "format_name": "matroska,webm",
        "duration": "120.500000",
        "size": "1048576",
        "tags": {"title": "Existing Title", "encoder": "libebml"},
    },
}

BINARIES = override_settings(
    FFMPEG_BINARY=sys.executable, FFPROBE_BINARY=sys.executable
)


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr=stderr)


def _writer(path: Path):
    """Mock for ``_run`` that behaves like FFmpeg: creates the output file."""

    def _run(args, timeout):
        path.write_bytes(b"written-by-fake-ffmpeg")
        return completed()

    return _run


class ProbeParsingTests(SimpleTestCase):
    def setUp(self):
        self._scratch = make_scratch_dir()
        self.addCleanup(remove_scratch_dir, self._scratch)
        self.sample = self._scratch / "movie.mkv"
        self.sample.write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 64)

    @BINARIES
    def test_probe_parses_container_and_streams(self):
        with mock.patch.object(media, "_run", return_value=completed(json.dumps(PROBE_JSON))):
            info = media.probe(self.sample)
        self.assertEqual(info.video_codec, "h264")
        self.assertEqual(info.audio_codec, "aac")
        self.assertEqual((info.width, info.height), (1920, 1080))
        self.assertEqual(info.frame_rate, 23.976)
        self.assertAlmostEqual(info.duration, 120.5)
        self.assertEqual(info.stream_count, 3)
        self.assertEqual(info.tags["title"], "Existing Title")
        # Subtitles are retained in the summary, not silently dropped.
        self.assertEqual([s["type"] for s in info.summary()], ["video", "audio", "subtitle"])

    @BINARIES
    def test_probe_failure_is_typed(self):
        with mock.patch.object(media, "_run", return_value=completed("", "Invalid data", 1)):
            with self.assertRaises(media.MediaToolingError) as ctx:
                media.probe(self.sample)
        self.assertEqual(ctx.exception.code, "probe_failed")
        self.assertIn("Invalid data", ctx.exception.detail)

    @BINARIES
    def test_probe_rejects_unreadable_json(self):
        with mock.patch.object(media, "_run", return_value=completed("not json")):
            with self.assertRaises(media.MediaToolingError) as ctx:
                media.probe(self.sample)
        self.assertEqual(ctx.exception.code, "probe_invalid_json")

    @BINARIES
    def test_probe_rejects_missing_and_empty_files(self):
        with self.assertRaises(media.MediaToolingError) as ctx:
            media.probe(self._scratch / "nope.mp4")
        self.assertEqual(ctx.exception.code, "missing_source")

        empty = self._scratch / "empty.mp4"
        empty.write_bytes(b"")
        with self.assertRaises(media.MediaToolingError) as ctx:
            media.probe(empty)
        self.assertEqual(ctx.exception.code, "empty_source")


class ToolingTests(SimpleTestCase):
    @override_settings(FFMPEG_BINARY="/definitely/not/here/ffmpeg")
    def test_missing_binary_is_reported(self):
        with self.assertRaises(media.MediaToolingError) as ctx:
            media.ffmpeg_path()
        self.assertEqual(ctx.exception.code, "tooling_missing")
        self.assertFalse(media.tooling_available())
        self.assertIn("tooling_missing", media.tooling_status()["error"])

    @override_settings(FFMPEG_BINARY="", FFPROBE_BINARY="")
    def test_unconfigured_binary_is_reported(self):
        with self.assertRaises(media.MediaToolingError):
            media.ffprobe_path()

    def test_timeout_is_mapped_to_a_retryable_code(self):
        with mock.patch.object(
            media.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5)
        ):
            with self.assertRaises(media.MediaToolingError) as ctx:
                media._run(["ffmpeg"], timeout=5)
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertTrue(ctx.exception.retryable)

    def test_run_never_uses_a_shell(self):
        with mock.patch.object(media.subprocess, "run", return_value=completed("ok")) as run:
            media._run(["ffmpeg", "-version"], timeout=5)
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertFalse(kwargs.get("shell", False))
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["timeout"], 5)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)

    def test_real_subprocess_execution_without_shell(self):
        """Proves argv execution works at all (no FFmpeg needed)."""
        result = media._run([sys.executable, "-c", "print('hello')"], timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertIn("hello", result.stdout)


class EmbedTests(SimpleTestCase):
    def setUp(self):
        self._scratch = make_scratch_dir()
        self.addCleanup(remove_scratch_dir, self._scratch)
        self.source = self._scratch / "source.mkv"
        self.source.write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 64)
        self.before = media.ProbeResult(
            path=str(self.source),
            size_bytes=1024,
            duration=100.0,
            width=1920,
            height=1080,
            frame_rate=24.0,
            video_codec="h264",
            audio_codec="aac",
            container="matroska,webm",
            tags={},
            streams=[{"codec_type": "video"}, {"codec_type": "audio"}],
        )

    @BINARIES
    def test_stream_copy_arguments_are_exact(self):
        after = media.ProbeResult(
            path="out.mkv", size_bytes=1024, duration=100.0, width=1920, height=1080,
            frame_rate=24.0, video_codec="h264", audio_codec="aac", container="matroska,webm",
            tags={"title": "Branded", "comment": "https://nzdworld.com"},
            streams=[{"codec_type": "video"}, {"codec_type": "audio"}],
        )
        with mock.patch.object(media, "probe", side_effect=[self.before, after]), \
             mock.patch.object(media, "_run", side_effect=_writer(self._scratch / "out.mkv")) as run:
            result = media.embed_metadata(
                self.source, self._scratch / "out.mkv", {"title": "Branded"}
            )
        argv = run.call_args[0][0]
        self.assertIn("-c", argv)
        self.assertEqual(argv[argv.index("-c") + 1], "copy")
        self.assertEqual(argv[argv.index("-map") + 1], "0")
        self.assertEqual(argv[argv.index("-map_metadata") + 1], "0")
        self.assertIn("-metadata", argv)
        self.assertIn("title=Branded", argv)
        self.assertNotIn("shell", run.call_args[1])
        self.assertTrue(result.streams_preserved)

    @BINARIES
    def test_unsupported_tags_are_reported_not_hidden(self):
        after = media.ProbeResult(
            path="out.webm", size_bytes=1024, duration=100.0, width=1920, height=1080,
            frame_rate=24.0, video_codec="vp9", audio_codec="opus", container="matroska,webm",
            tags={"title": "Branded"},  # artist/album refused by WebM
            streams=[{"codec_type": "video"}, {"codec_type": "audio"}],
        )
        before_webm = media.ProbeResult(
            path=str(self.source), size_bytes=1024, duration=100.0, width=1920, height=1080,
            frame_rate=24.0, video_codec="vp9", audio_codec="opus", container="matroska,webm",
            tags={}, streams=[{"codec_type": "video"}, {"codec_type": "audio"}],
        )
        with mock.patch.object(media, "probe", side_effect=[before_webm, after]), \
             mock.patch.object(media, "_run", side_effect=_writer(self._scratch / "out.webm")):
            result = media.embed_metadata(
                self.source,
                self._scratch / "out.webm",
                {"title": "Branded", "artist": "NZDWorld.com"},
                extension="webm",
            )
        self.assertEqual(result.written, {"title": "Branded"})
        self.assertEqual(result.unsupported, ["artist"])

    @BINARIES
    def test_failed_copy_raises_and_leaves_no_output(self):
        with mock.patch.object(media, "probe", return_value=self.before), \
             mock.patch.object(media, "_run", return_value=completed("", "boom", 1)):
            with self.assertRaises(media.MediaToolingError) as ctx:
                media.embed_metadata(self.source, self._scratch / "out.mkv", {"title": "x"})
        self.assertEqual(ctx.exception.code, "embed_failed")


class StreamComparisonTests(SimpleTestCase):
    def _probe(self, **kwargs):
        base = dict(
            path="p", size_bytes=1, duration=100.0, width=1920, height=1080,
            frame_rate=24.0, video_codec="h264", audio_codec="aac",
            container="matroska", tags={}, streams=[{"codec_type": "video"}],
        )
        base.update(kwargs)
        return media.ProbeResult(**base)

    def test_identical_streams_report_no_mismatch(self):
        self.assertEqual(media.compare_streams(self._probe(), self._probe()), [])

    def test_codec_change_is_detected(self):
        mismatches = media.compare_streams(self._probe(), self._probe(video_codec="hevc"))
        self.assertTrue(any("video codec" in item for item in mismatches))

    def test_resolution_change_is_detected(self):
        mismatches = media.compare_streams(self._probe(), self._probe(height=720))
        self.assertTrue(any("resolution" in item for item in mismatches))

    def test_small_duration_rounding_is_tolerated(self):
        self.assertEqual(
            media.compare_streams(self._probe(), self._probe(duration=100.4)), []
        )

    def test_large_duration_change_is_detected(self):
        mismatches = media.compare_streams(self._probe(), self._probe(duration=140.0))
        self.assertTrue(any("duration" in item for item in mismatches))

    def test_stream_count_change_is_detected(self):
        mismatches = media.compare_streams(
            self._probe(), self._probe(streams=[{"codec_type": "video"}, {"codec_type": "audio"}])
        )
        self.assertTrue(any("stream count" in item for item in mismatches))


class ThumbnailTests(SimpleTestCase):
    def _info(self, duration):
        return media.ProbeResult(
            path="p", size_bytes=1, duration=duration, width=1920, height=1080,
            frame_rate=24.0, video_codec="h264", audio_codec="aac",
            container="mp4", tags={}, streams=[{"codec_type": "video"}],
        )

    def test_default_time_avoids_the_very_start_and_the_end(self):
        self.assertGreater(media.default_thumbnail_time(self._info(100.0)), 5)
        self.assertLess(media.default_thumbnail_time(self._info(100.0)), 100)
        self.assertEqual(media.default_thumbnail_time(self._info(0.5)), 0.0)

    @BINARIES
    def test_thumbnail_retries_at_zero_when_the_seek_fails(self):
        scratch = make_scratch_dir()
        self.addCleanup(remove_scratch_dir, scratch)
        source = scratch / "src.mp4"
        source.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        destination = scratch / "poster.jpg"

        def fake_run(args, timeout):
            # First attempt fails and produces nothing; second succeeds.
            if destination.exists():
                return completed()
            destination.write_bytes(b"jpegdata")
            return completed("", "seek failed", 1)

        with mock.patch.object(media, "probe", return_value=self._info(100.0)), \
             mock.patch.object(media, "_run", side_effect=fake_run):
            result = media.make_thumbnail(source, destination)
        self.assertEqual(result.at_seconds, 0.0)
        self.assertEqual(Path(result.path).read_bytes(), b"jpegdata")

    @BINARIES
    def test_thumbnail_failure_is_typed(self):
        scratch = make_scratch_dir()
        self.addCleanup(remove_scratch_dir, scratch)
        source = scratch / "src.mp4"
        source.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        with mock.patch.object(media, "probe", return_value=self._info(100.0)), \
             mock.patch.object(media, "_run", return_value=completed("", "nope", 1)):
            with self.assertRaises(media.MediaToolingError) as ctx:
                media.make_thumbnail(source, scratch / "poster.jpg")
        self.assertEqual(ctx.exception.code, "thumbnail_failed")


class TranscodeGuardTests(SimpleTestCase):
    def test_transcode_is_refused_by_default(self):
        with override_settings(ALLOW_TRANSCODE=False):
            with self.assertRaises(media.MediaToolingError) as ctx:
                media.transcode("in.mp4", "out.mp4")
        self.assertEqual(ctx.exception.code, "transcode_disabled")


@skipUnless(media.tooling_available(), "FFmpeg/FFprobe not installed on this machine")
class FfmpegIntegrationTests(SimpleTestCase):
    """Runs only where FFmpeg exists — this is the suite that must pass on the server."""

    def setUp(self):
        self.work = make_scratch_dir()
        self.addCleanup(remove_scratch_dir, self.work)
        self.sample = self.work / "sample.mp4"
        media._run(  # noqa: SLF001
            [
                media.ffmpeg_path(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                str(self.sample),
            ],
            timeout=120,
        )

    def test_probe_reads_real_metadata(self):
        info = media.probe(self.sample)
        self.assertEqual(info.video_codec, "h264")
        self.assertEqual(info.audio_codec, "aac")
        self.assertEqual((info.width, info.height), (320, 240))
        self.assertGreater(info.duration or 0, 1.0)

    def test_stream_copy_preserves_everything_and_writes_tags(self):
        out = self.work / "branded.mp4"
        result = media.embed_metadata(
            self.sample, out, {"title": "Integration | NZDWorld.com"}, extension="mp4"
        )
        self.assertEqual(result.mismatches, [])
        self.assertIn("title", result.written)
        self.assertEqual(result.probe.video_codec, "h264")

    def test_thumbnail_is_a_real_image(self):
        thumb = media.make_thumbnail(self.sample, self.work / "poster.jpg")
        data = Path(thumb.path).read_bytes()
        self.assertTrue(data.startswith(b"\xff\xd8"), "JPEG magic bytes expected")
        self.assertGreater(len(data), 1000)
