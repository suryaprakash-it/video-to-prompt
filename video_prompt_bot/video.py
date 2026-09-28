"""Video probing, input validation, frame extraction, and temporary cleanup."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import shutil
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Sequence

logger = logging.getLogger(__name__)
VIDEO_SUFFIXES = {".3gp", ".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm"}


class VideoProcessingError(RuntimeError):
    """Raised for invalid or unreadable video input and FFmpeg failures."""


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    """Useful video stream metadata reported by FFprobe."""

    duration_seconds: float
    width: int
    height: int
    frame_rate: float | None
    size_bytes: int | None


def validate_video_upload(
    file_name: str | None,
    mime_type: str | None,
    file_size: int | None,
    max_file_size_bytes: int,
) -> None:
    """Reject unsupported, empty, or oversized Telegram media before downloading."""
    suffix = Path(file_name or "").suffix.lower()
    is_video = bool(mime_type and mime_type.lower().startswith("video/")) or suffix in VIDEO_SUFFIXES
    if not is_video:
        raise VideoProcessingError("Please send a video file such as MP4, MOV, MKV, or WebM.")
    if file_size is not None and file_size <= 0:
        raise VideoProcessingError("The uploaded video is empty.")
    if file_size is not None and file_size > max_file_size_bytes:
        limit_mb = max_file_size_bytes // (1024 * 1024)
        raise VideoProcessingError(f"The video is larger than this bot's {limit_mb} MB limit.")


def _parse_frame_rate(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        parsed = float(Fraction(value))
    except (OverflowError, ValueError, ZeroDivisionError):
        return None
    return parsed if parsed > 0 and math.isfinite(parsed) else None


def parse_probe_output(raw_json: str) -> VideoMetadata:
    """Parse FFprobe JSON and require a valid video stream and duration."""
    try:
        payload: dict[str, Any] = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise VideoProcessingError("FFprobe returned invalid metadata.") from exc

    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise VideoProcessingError("No video stream was found in the uploaded file.")
    stream = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"), None)
    if stream is None:
        raise VideoProcessingError("No video stream was found in the uploaded file.")

    format_info = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration_value = format_info.get("duration", stream.get("duration"))
    try:
        duration = float(duration_value)
        width = int(stream.get("width", 0))
        height = int(stream.get("height", 0))
    except (TypeError, ValueError) as exc:
        raise VideoProcessingError("Video metadata is incomplete or invalid.") from exc
    if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
        raise VideoProcessingError("The video has an invalid duration or frame size.")

    size: int | None
    try:
        size = int(format_info["size"]) if format_info.get("size") is not None else None
    except (TypeError, ValueError):
        size = None

    return VideoMetadata(
        duration_seconds=duration,
        width=width,
        height=height,
        frame_rate=_parse_frame_rate(stream.get("avg_frame_rate")),
        size_bytes=size,
    )


def generate_frame_timestamps(duration_seconds: float, frame_count: int) -> list[float]:
    """Return evenly spaced sample times inside a video's duration."""
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be greater than zero")
    if frame_count <= 0:
        raise ValueError("frame_count must be greater than zero")
    return [duration_seconds * index / (frame_count + 1) for index in range(1, frame_count + 1)]


async def _run_process(args: Sequence[str], timeout_seconds: float) -> tuple[int, bytes, bytes]:
    """Run a child process asynchronously and reliably stop it on timeout."""
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise VideoProcessingError(f"Required executable was not found: {args[0]}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise VideoProcessingError(f"Video tool timed out after {timeout_seconds:g} seconds.") from exc
    return process.returncode or 0, stdout, stderr


async def probe_video(path: Path, ffprobe_bin: str = "ffprobe") -> VideoMetadata:
    """Run FFprobe and extract duration, dimensions, frame rate, and file size."""
    command = [
        ffprobe_bin,
        "-v", "error",
        "-show_entries", "format=duration,size:stream=codec_type,width,height,avg_frame_rate,duration",
        "-of", "json",
        str(path),
    ]
    code, stdout, stderr = await _run_process(command, timeout_seconds=30)
    if code:
        details = stderr.decode("utf-8", errors="replace").strip()
        logger.warning("ffprobe failed (%s): %s", code, details[:500])
        raise VideoProcessingError("FFprobe could not read this video. Please send a valid video file.")
    return parse_probe_output(stdout.decode("utf-8", errors="replace"))


async def download_telegram_video(
    client: Any,
    message: Any,
    target_path: Path,
    timeout_seconds: int = 300,
    progress_callback: Callable[[int, int], Any] | None = None,
) -> Path:
    """Download a Telegram video with progress reporting and a bounded wait."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    options: dict[str, Any] = {"file_name": str(target_path)}
    if progress_callback is not None:
        options["progress"] = progress_callback
    try:
        result = await asyncio.wait_for(
            client.download_media(message, **options),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise VideoProcessingError(
            f"Telegram did not finish downloading the video within {timeout_seconds} seconds. "
            "Please retry or send a smaller video."
        ) from exc
    if not result:
        raise VideoProcessingError("Telegram could not download this video. Please try again.")
    return Path(result)


async def extract_frames(
    video_path: Path,
    output_dir: Path,
    duration_seconds: float,
    frame_count: int = 24,
    ffmpeg_bin: str = "ffmpeg",
) -> list[Path]:
    """Extract a bounded set of resized JPEG frames in temporal order."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamps = generate_frame_timestamps(duration_seconds, frame_count)
    frame_paths: list[Path] = []
    for index, timestamp in enumerate(timestamps, 1):
        output_path = output_dir / f"frame_{index:02d}.jpg"
        command = [
            ffmpeg_bin,
            "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{timestamp:.3f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease",
            "-q:v", "4",
            str(output_path),
        ]
        code, _stdout, stderr = await _run_process(command, timeout_seconds=60)
        if code or not output_path.is_file() or output_path.stat().st_size == 0:
            details = stderr.decode("utf-8", errors="replace").strip()
            logger.warning("ffmpeg frame %d failed (%s): %s", index, code, details[:500])
            raise VideoProcessingError("FFmpeg could not extract frames from this video.")
        frame_paths.append(output_path)
    return frame_paths


def cleanup_temp_path(path: Path) -> None:
    """Remove a temporary file or directory, including on partial failures."""
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
    except FileNotFoundError:
        return
