"""
Self-test for the FFmpeg integration, runnable inside the Django environment.

    python manage.py media_selftest

It generates a real sample clip with the configured FFmpeg, then exercises every
operation the importer depends on (probe, metadata stream-copy, poster frame,
optional transcode) and prints a pass/fail report. This is the command to run
on the server after deploying, because it proves the *application user's*
environment, not just an SSH shell.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from uploader.services import media


class Command(BaseCommand):
    help = "Verify that FFmpeg/FFprobe work from the Django application environment."

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep",
            action="store_true",
            help="Keep the generated sample files for inspection.",
        )

    def handle(self, *args, **options):
        status = media.tooling_status()
        self.stdout.write("FFmpeg path : " + status["ffmpeg"])
        self.stdout.write("FFprobe path: " + status["ffprobe"])
        if not status["available"]:
            raise CommandError(f"Tooling unavailable: {status['error']}")
        self.stdout.write("FFmpeg      : " + status["ffmpeg_version"])
        self.stdout.write("FFprobe     : " + status["ffprobe_version"])

        work = Path(settings.TEMP_ROOT) / "selftest"
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)

        failures: list[str] = []
        try:
            sample = work / "sample.mp4"
            self._step("generate sample clip (H.264 + AAC)", failures, self._make_sample, sample)
            if not sample.exists():
                raise CommandError("Could not generate the sample clip; aborting.")

            info = None
            try:
                info = media.probe(sample)
                self.stdout.write(
                    f"  OK  probe: {info.video_codec}/{info.audio_codec} "
                    f"{info.width}x{info.height} {info.frame_rate}fps "
                    f"{info.duration}s container={info.container}"
                )
            except media.MediaToolingError as exc:
                failures.append(f"probe: {exc.code} {exc.message}")

            branded = work / "sample.branded.mp4"
            if info is not None:
                try:
                    result = media.embed_metadata(
                        sample,
                        branded,
                        {"title": "Self test | NZDWorld.com", "comment": "https://nzdworld.com"},
                        extension="mp4",
                    )
                    if result.mismatches:
                        failures.append("stream copy changed streams: " + "; ".join(result.mismatches))
                    else:
                        self.stdout.write(
                            f"  OK  stream copy: streams preserved, tags written={sorted(result.written)} "
                            f"unsupported={result.unsupported}"
                        )
                except media.MediaToolingError as exc:
                    failures.append(f"embed: {exc.code} {exc.message}")

            thumb_source = branded if branded.exists() else sample
            if info is not None:
                try:
                    thumb = media.make_thumbnail(thumb_source, work / "poster.jpg")
                    size = Path(thumb.path).stat().st_size
                    self.stdout.write(
                        f"  OK  thumbnail: {size} bytes at {thumb.at_seconds}s (width {thumb.width})"
                    )
                except media.MediaToolingError as exc:
                    failures.append(f"thumbnail: {exc.code} {exc.message}")

            if getattr(settings, "ALLOW_TRANSCODE", False):
                try:
                    out = media.transcode(sample, work / "sample.transcoded.mp4")
                    self.stdout.write(f"  OK  transcode: {out.video_codec}/{out.audio_codec}")
                except media.MediaToolingError as exc:
                    failures.append(f"transcode: {exc.code} {exc.message}")
            else:
                self.stdout.write("  --  transcode skipped (ALLOW_TRANSCODE is False)")

        finally:
            if not options["keep"]:
                shutil.rmtree(work, ignore_errors=True)
            else:
                self.stdout.write(f"  ..  artefacts kept in {work}")

        if failures:
            raise CommandError("media_selftest FAILED:\n  - " + "\n  - ".join(failures))
        self.stdout.write(self.style.SUCCESS("media_selftest PASSED"))

    def _step(self, label: str, failures: list[str], func, *args) -> None:
        try:
            func(*args)
            self.stdout.write(f"  OK  {label}")
        except media.MediaToolingError as exc:
            failures.append(f"{label}: {exc.code} {exc.message}")

    def _make_sample(self, path: Path) -> None:
        """Deterministic 2-second clip, generated by FFmpeg itself."""
        args = [
            media.ffmpeg_path(),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ]
        completed = media._run(args, timeout=120)  # noqa: SLF001 - intentional selftest
        if completed.returncode != 0 or not path.exists():
            raise media.MediaToolingError(
                "sample_generation_failed",
                "FFmpeg could not generate the sample clip.",
                detail=media._sanitise(completed.stderr),  # noqa: SLF001
            )
