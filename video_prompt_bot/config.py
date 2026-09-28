"""Environment and .env configuration for the video prompt bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ConfigurationError(ValueError):
    """Raised when required settings are missing or invalid."""


@dataclass(frozen=True, slots=True)
class BotConfig:
    """Validated runtime configuration."""

    api_id: int
    api_hash: str
    bot_token: str
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"
    database_path: Path = Path("data/settings.sqlite3")
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    max_video_size_mb: int = 100
    max_video_duration_seconds: int = 180
    frame_count: int = 24
    download_timeout_seconds: int = 300


def _parse_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE dotenv entries without overriding process env."""
    if not path.is_file():
        return {}

    values: dict[str, str] = {}
    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigurationError(f"Could not read environment file: {path}") from exc
    for line_number, raw_line in enumerate(contents.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(f"Invalid .env entry on line {line_number}: expected KEY=VALUE")
        key, value = (piece.strip() for piece in line.split("=", 1))
        if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            raise ConfigurationError(f"Invalid .env variable name on line {line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def _positive_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name, str(default)).strip()
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be a positive integer")
    return parsed


def load_config(
    env: Mapping[str, str] | None = None,
    env_file: str | Path | None = Path(".env"),
) -> BotConfig:
    """Load and validate configuration, with process variables taking precedence."""
    file_values = _parse_env_file(Path(env_file)) if env_file is not None else {}
    source = dict(os.environ if env is None else env)
    values = {**file_values, **source}

    required = ("API_ID", "API_HASH", "BOT_TOKEN", "OPENAI_API_KEY")
    missing = [name for name in required if not values.get(name, "").strip()]
    if missing:
        raise ConfigurationError("Missing required environment variable(s): " + ", ".join(missing))

    try:
        api_id = int(values["API_ID"].strip())
    except ValueError as exc:
        raise ConfigurationError("API_ID must be a positive integer") from exc
    if api_id <= 0:
        raise ConfigurationError("API_ID must be a positive integer")

    frame_count = _positive_int(values, "FRAME_COUNT", 24)
    if frame_count > 24:
        raise ConfigurationError("FRAME_COUNT cannot exceed 24")

    return BotConfig(
        api_id=api_id,
        api_hash=values["API_HASH"].strip(),
        bot_token=values["BOT_TOKEN"].strip(),
        openai_api_key=values["OPENAI_API_KEY"].strip(),
        openai_model=values.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
        database_path=Path(values.get("DATABASE_PATH", "").strip() or "data/settings.sqlite3"),
        ffmpeg_bin=values.get("FFMPEG_BIN", "ffmpeg").strip() or "ffmpeg",
        ffprobe_bin=values.get("FFPROBE_BIN", "ffprobe").strip() or "ffprobe",
        max_video_size_mb=_positive_int(values, "MAX_VIDEO_SIZE_MB", 100),
        max_video_duration_seconds=_positive_int(values, "MAX_VIDEO_DURATION_SECONDS", 180),
        frame_count=frame_count,
        download_timeout_seconds=_positive_int(values, "DOWNLOAD_TIMEOUT_SECONDS", 300),
    )
