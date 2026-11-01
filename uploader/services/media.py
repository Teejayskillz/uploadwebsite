"""
FFmpeg / FFprobe integration (brief section 6).

Design constraints that shaped this module
------------------------------------------
* **Absolute binary paths.** Passenger does not inherit an interactive shell's
  ``PATH``, so ``"ffmpeg"`` is not a safe command name under the web server.
  Paths come from ``settings.FFMPEG_BINARY`` / ``FFPROBE_BINARY``.
* **No shell, ever.** Every call is an argv list executed with
  ``subprocess.run([...], shell=False)`` (the default), so a filename can never
  become a command.
* **Nothing is re-encoded by default.** Branding uses ``-map 0 -c copy`` so
  resolution, frame rate, codecs and streams survive untouched. Re-encoding only
  happens through :func:`transcode`, which is gated by ``ALLOW_TRANSCODE``.
* **Success is verified, not assumed.** After a stream copy the output is
  probed again and compared with the input; tags that the container refuses are
  reported as unsupported rather than silently declared written.
* **Every call has a timeout**, and a timeout/failure raises a typed error with a
  stable code so the queue can store something meaningful.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings

logger = logging.getLogger("uploader.media")

# Metadata the uploader wants on every stored file.
BRAND_METADATA = {
    "title": "",  # filled per import
    "comment": "https://nzdworld.com",
    "copyright": "NZDWorld.com",
    "artist": "NZDWorld.com",
    "album": "NZDWorld.com",
}

# What each container can *usually* carry. This is only a hint: the authoritative
# answer comes from re-probing the output (see EmbedResult.unsupported).
_CONTAINER_TAGS = {
    "mp4": {"title", "comment", "copyright", "artist", "album", "genre", "date", "description"},
    "mov": {"title", "comment", "copyright", "artist", "album", "genre", "date", "description"},
    "m4v": {"title", "comment", "copyright", "artist", "album", "genre", "date", "description"},
    "mkv": {"title", "comment", "copyright", "artist", "album", "genre", "date", "description", "synopsis"},
    "webm": {"title", "comment", "copyright", "date", "description"},
    "avi": {"title", "comment", "copyright", "artist"},
    "flv": {"title", "comment"},
    "ts": set(),
    "mpg": set(),
}

MAX_STDERR_CHARS = 600


class MediaToolingError(Exception):
    """A media subprocess failed or the tooling is unavailable."""

    def __init__(self, code: str, message: str = "", *, detail: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.detail = detail

    @property
    def retryable(self) -> bool:
        return self.code in {"timeout", "exec_failed"}


@dataclass(frozen=True)
class ProbeResult:
    path: str
    size_bytes: int
    duration: float | None
    width: int | None
    height: int | None
    frame_rate: float | None
    video_codec: str
    audio_codec: str
    container: str
    tags: dict
    streams: list[dict] = field(default_factory=list)
    raw_format: dict = field(default_factory=dict)

    @property
    def stream_count(self) -> int:
        return len(self.streams)

    def summary(self) -> dict:
        """Compact, database-friendly subset stored on ``Video.streams``."""
        return [
            {
                "type": stream.get("codec_type"),
                "codec": stream.get("codec_name"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "channels": stream.get("channels"),
                "language": (stream.get("tags") or {}).get("language"),
            }
            for stream in self.streams
        ]


@dataclass(frozen=True)
class EmbedResult:
    path: str
    probe: ProbeResult
    requested: dict
    written: dict
    unsupported: list[str]
    mismatches: list[str]

    @property
    def streams_preserved(self) -> bool:
        return not self.mismatches


@dataclass(frozen=True)
class ThumbResult:
    path: str
    at_seconds: float
    width: int


# ---------------------------------------------------------------------------
# Tooling discovery
# ---------------------------------------------------------------------------
def _tool(path_setting: str, label: str) -> str:
    raw = str(path_setting or "").strip()
    if not raw:
        raise MediaToolingError("tooling_missing", f"{label} path is not configured.")
    candidate = Path(raw)
    if not candidate.is_file():
        raise MediaToolingError(
            "tooling_missing", f"{label} not found at {candidate}.", detail=str(candidate)
        )
    if not os.access(candidate, os.X_OK):
        raise MediaToolingError(
            "tooling_not_executable",
            f"{label} at {candidate} is not executable.",
            detail=str(candidate),
        )
    return str(candidate)


def ffmpeg_path() -> str:
    return _tool(settings.FFMPEG_BINARY, "FFmpeg")


def ffprobe_path() -> str:
    return _tool(settings.FFPROBE_BINARY, "FFprobe")


def tooling_available() -> bool:
    """Cheap boolean used by callers that may degrade gracefully."""
    try:
        ffmpeg_path()
        ffprobe_path()
        return True
    except MediaToolingError:
        return False


def tooling_status() -> dict:
    """Human-readable status for the dashboard / selftest command."""
    status = {
        "ffmpeg": str(settings.FFMPEG_BINARY),
        "ffprobe": str(settings.FFPROBE_BINARY),
        "available": False,
        "ffmpeg_version": "",
        "ffprobe_version": "",
        "error": "",
    }
    try:
        ffmpeg = ffmpeg_path()
        ffprobe = ffprobe_path()
    except MediaToolingError as exc:
        status["error"] = f"{exc.code}: {exc.message}"
        return status
    for key, binary in (("ffmpeg_version", ffmpeg), ("ffprobe_version", ffprobe)):
        try:
            completed = _run([binary, "-version"], timeout=20)
            first = (completed.stdout or "").splitlines()[:1]
            status[key] = first[0].strip() if first else ""
        except MediaToolingError as exc:
            status["error"] = f"{exc.code}: {exc.message}"
            return status
    status["available"] = True
    return status


# ---------------------------------------------------------------------------
# Subprocess plumbing
# ---------------------------------------------------------------------------
def _sanitise(text: str | bytes | None) -> str:
    if not text:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = text.replace("\x00", "")
    return text.strip()[-MAX_STDERR_CHARS:]


def _run(args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    """
    Execute an argv list. Never uses a shell, never inherits stdin.

    ``subprocess.run`` with ``timeout`` kills the child on expiry; we translate
    that into a typed error so the queue can record ``timeout``.
    """
    logger.info("media exec: %s", " ".join(args[:3]))
    try:
        return subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaToolingError(
            "timeout", f"Command exceeded {timeout}s.", detail=_sanitise(exc.output)
        ) from exc
    except (FileNotFoundError, PermissionError) as exc:
        raise MediaToolingError("tooling_missing", f"Cannot execute media tool: {exc}") from exc
    except OSError as exc:
        raise MediaToolingError("exec_failed", f"Execution failed: {exc}") from exc


def _require_file(path: Path, label: str) -> Path:
    path = Path(path)
    if not path.is_file():
        raise MediaToolingError("missing_source", f"{label} does not exist: {path}")
    if path.stat().st_size <= 0:
        raise MediaToolingError("empty_source", f"{label} is empty: {path}")
    return path


def _fraction_to_float(value: str | None) -> float | None:
    if not value:
        return None
    match = re.fullmatch(r"(\d+)/(\d+)", str(value).strip())
    if match:
        numerator, denominator = int(match.group(1)), int(match.group(2))
        if denominator == 0:
            return None
        return round(numerator / denominator, 3)
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _as_float(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _as_int(value) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------
def probe(path: str | Path, *, timeout: int | None = None) -> ProbeResult:
    """Read container/stream metadata. Read-only, never modifies the file."""
    source = _require_file(Path(path), "Media file")
    args = [
        ffprobe_path(),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(source),
    ]
    completed = _run(args, timeout=timeout or settings.FFPROBE_TIMEOUT_SECONDS)
    if completed.returncode != 0:
        raise MediaToolingError(
            "probe_failed",
            "FFprobe could not read this file.",
            detail=_sanitise(completed.stderr),
        )
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaToolingError("probe_invalid_json", "FFprobe returned unreadable JSON.") from exc

    streams = payload.get("streams") or []
    format_info = payload.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})

    duration = _as_float(format_info.get("duration")) or _as_float(video.get("duration"))
    return ProbeResult(
        path=str(source),
        size_bytes=int(format_info.get("size") or source.stat().st_size),
        duration=duration,
        width=_as_int(video.get("width")),
        height=_as_int(video.get("height")),
        frame_rate=_fraction_to_float(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        video_codec=str(video.get("codec_name") or ""),
        audio_codec=str(audio.get("codec_name") or ""),
        container=str(format_info.get("format_name") or ""),
        tags={str(k).lower(): str(v) for k, v in (format_info.get("tags") or {}).items()},
        streams=streams,
        raw_format=format_info,
    )


# ---------------------------------------------------------------------------
# Stream copy + metadata
# ---------------------------------------------------------------------------
def container_key(probe_result: ProbeResult, extension: str = "") -> str:
    """Best single container name, e.g. ``matroska,webm`` -> ``webm``."""
    ext = (extension or "").lower().lstrip(".")
    if ext:
        return ext
    names = [part for part in probe_result.container.split(",") if part]
    if not names:
        return ""
    if len(names) > 1 and "webm" in names:
        return "webm"
    return names[0]


def supported_tags(container: str) -> set[str]:
    return set(_CONTAINER_TAGS.get(container.lower(), set()))


def compare_streams(before: ProbeResult, after: ProbeResult) -> list[str]:
    """
    Differences that mean the stream copy changed the video.

    A one-second duration tolerance covers container rounding, which is expected
    and not a re-encode.
    """
    mismatches: list[str] = []
    if before.stream_count != after.stream_count:
        mismatches.append(f"stream count {before.stream_count} -> {after.stream_count}")
    if before.video_codec != after.video_codec:
        mismatches.append(f"video codec {before.video_codec} -> {after.video_codec}")
    if before.audio_codec != after.audio_codec:
        mismatches.append(f"audio codec {before.audio_codec} -> {after.audio_codec}")
    if before.width != after.width or before.height != after.height:
        mismatches.append(
            f"resolution {before.width}x{before.height} -> {after.width}x{after.height}"
        )
    if before.frame_rate and after.frame_rate:
        if abs(before.frame_rate - after.frame_rate) > 0.01:
            mismatches.append(f"frame rate {before.frame_rate} -> {after.frame_rate}")
    if before.duration and after.duration:
        if abs(before.duration - after.duration) > 1.0:
            mismatches.append(f"duration {before.duration} -> {after.duration}")
    return mismatches


def embed_metadata(
    source: str | Path,
    destination: str | Path,
    fields: dict[str, str],
    *,
    extension: str = "",
    timeout: int | None = None,
) -> EmbedResult:
    """
    Copy every stream, changing only container-level tags.

    The source file is never modified: the caller keeps it until this output has
    been probed and accepted.
    """
    src = _require_file(Path(source), "Source video")
    dst = Path(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()

    before = probe(src)
    requested = {str(k): str(v) for k, v in (fields or {}).items() if str(v).strip()}

    args = [
        ffmpeg_path(),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-map",
        "0",  # every stream, not just the default
        "-map_metadata",
        "0",  # keep existing tags (title, encoder, chapters, ...)
        "-c",
        "copy",  # never re-encode
    ]
    for key, value in requested.items():
        args.extend(["-metadata", f"{key}={value}"])

    container = container_key(before, extension or dst.suffix)
    if container in {"mp4", "mov", "m4v"}:
        # Web-friendly moov placement; still a stream copy.
        args.extend(["-movflags", "+faststart"])
    args.append(str(dst))

    completed = _run(args, timeout=timeout or settings.FFMPEG_TIMEOUT_SECONDS)
    if completed.returncode != 0 or not dst.exists():
        raise MediaToolingError(
            "embed_failed",
            "FFmpeg could not write the branded copy.",
            detail=_sanitise(completed.stderr),
        )

    after = probe(dst)
    mismatches = compare_streams(before, after)
    declared_supported = supported_tags(container)
    written = {key: after.tags[key] for key in requested if key in after.tags}
    unsupported = [key for key in requested if key not in after.tags]

    return EmbedResult(
        path=str(dst),
        probe=after,
        requested=requested,
        written=written,
        unsupported=unsupported,
        mismatches=mismatches,
    )


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------
def default_thumbnail_time(probe_result: ProbeResult) -> float:
    """A frame a little way in: avoids black intros, never past the end."""
    duration = probe_result.duration or 0.0
    if duration <= 1:
        return 0.0
    return round(min(max(duration * 0.1, 1.0), max(duration - 0.5, 0.0)), 2)


def make_thumbnail(
    source: str | Path,
    destination: str | Path,
    *,
    at_seconds: float | None = None,
    width: int | None = None,
    timeout: int | None = None,
) -> ThumbResult:
    """
    Write one poster frame as JPEG.

    ``-ss`` before ``-i`` is the fast seek path; if the chosen timestamp fails,
    the function retries at 0 rather than failing the whole import.
    """
    src = _require_file(Path(source), "Source video")
    dst = Path(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    width = int(width or settings.THUMBNAIL_WIDTH)
    chosen = at_seconds if at_seconds is not None else default_thumbnail_time(info)

    def _attempt(timestamp: float) -> subprocess.CompletedProcess:
        args = [
            ffmpeg_path(),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:-2:flags=lanczos",
            "-q:v",
            "3",
            str(dst),
        ]
        return _run(args, timeout=timeout or settings.THUMBNAIL_TIMEOUT_SECONDS)

    completed = _attempt(chosen)
    if (completed.returncode != 0 or not dst.exists() or dst.stat().st_size == 0) and chosen > 0:
        completed = _attempt(0.0)
        chosen = 0.0

    if completed.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise MediaToolingError(
            "thumbnail_failed",
            "FFmpeg could not produce a poster frame.",
            detail=_sanitise(completed.stderr),
        )
    return ThumbResult(path=str(dst), at_seconds=float(chosen), width=width)


# ---------------------------------------------------------------------------
# Explicit conversion (opt-in only)
# ---------------------------------------------------------------------------
def transcode(
    source: str | Path,
    destination: str | Path,
    *,
    crf: int | None = None,
    preset: str | None = None,
    timeout: int | None = None,
) -> ProbeResult:
    """
    Re-encode to H.264/AAC MP4.

    Deliberately **not** used by the normal import path. Only call this when the
    administrator has enabled ``ALLOW_TRANSCODE`` and chosen to change the video,
    because it permanently loses quality relative to the original.
    """
    if not getattr(settings, "ALLOW_TRANSCODE", False):
        raise MediaToolingError(
            "transcode_disabled",
            "Re-encoding is disabled. Enable ALLOW_TRANSCODE to permit conversion.",
        )
    src = _require_file(Path(source), "Source video")
    dst = Path(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()

    args = [
        ffmpeg_path(),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-c:v",
        "libx264",
        "-preset",
        str(preset or settings.TRANSCODE_PRESET),
        "-crf",
        str(int(crf if crf is not None else settings.TRANSCODE_CRF)),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    completed = _run(args, timeout=timeout or settings.FFMPEG_TIMEOUT_SECONDS)
    if completed.returncode != 0 or not dst.exists():
        raise MediaToolingError(
            "transcode_failed", "FFmpeg could not convert the video.", detail=_sanitise(completed.stderr)
        )
    return probe(dst)
