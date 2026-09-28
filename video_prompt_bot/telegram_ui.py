"""Pyrogram settings menu and message handlers."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from openai import OpenAIError
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import BotConfig
from .prompt import analyze_video_frames, format_analysis_response, split_telegram_message
from .settings import (
    ANALYSIS_DETAILS,
    OUTPUT_LANGUAGES,
    PROMPT_STYLES,
    SQLiteSettingsStore,
    UserPreferences,
)
from .video import (
    VideoProcessingError,
    cleanup_temp_path,
    download_telegram_video,
    extract_frames,
    generate_frame_timestamps,
    probe_video,
    validate_video_upload,
)

logger = logging.getLogger(__name__)
_SETTING_FIELDS = {
    "style": ("prompt_style", "🎬 Prompt Style", PROMPT_STYLES),
    "detail": ("analysis_detail", "📊 Analysis Detail", ANALYSIS_DETAILS),
    "language": ("output_language", "🌐 Output Language", OUTPUT_LANGUAGES),
}


def _overview_keyboard(preferences: UserPreferences) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎬 Prompt Style: {preferences.prompt_style}", callback_data="settings:style")],
        [InlineKeyboardButton(f"📊 Analysis Detail: {preferences.analysis_detail}", callback_data="settings:detail")],
        [InlineKeyboardButton(f"🌐 Output Language: {preferences.output_language}", callback_data="settings:language")],
        [InlineKeyboardButton("Done", callback_data="settings:done")],
    ])


def _choice_keyboard(category: str, preferences: UserPreferences) -> InlineKeyboardMarkup:
    key, _label, options = _SETTING_FIELDS[category]
    current = getattr(preferences, key)
    rows = [
        [InlineKeyboardButton(
            f"{'✅ ' if option == current else ''}{option}",
            callback_data=f"set:{key}:{option}",
        )]
        for option in options
    ]
    rows.append([InlineKeyboardButton("‹ Back to settings", callback_data="settings:open")])
    return InlineKeyboardMarkup(rows)


def _settings_summary(preferences: UserPreferences, saved: bool = False) -> str:
    prefix = "Settings saved.\n\n" if saved else "Choose a setting to change.\n\n"
    return (
        prefix
        + f"🎬 Prompt Style: {preferences.prompt_style}\n"
        + f"📊 Analysis Detail: {preferences.analysis_detail}\n"
        + f"🌐 Output Language: {preferences.output_language}"
    )


def _media_attributes(message: Any) -> tuple[str | None, str | None, int | None]:
    media = getattr(message, "video", None) or getattr(message, "document", None)
    if media is None:
        return None, None, None
    return (
        getattr(media, "file_name", None),
        getattr(media, "mime_type", None),
        getattr(media, "file_size", None),
    )


async def _send_text(client: Client, chat_id: int, text: str, reply_to_message_id: int | None = None) -> None:
    for part in split_telegram_message(text):
        await client.send_message(chat_id, part, reply_to_message_id=reply_to_message_id)


def register_handlers(
    app: Client,
    config: BotConfig,
    store: SQLiteSettingsStore,
    ai_client: AsyncOpenAI,
) -> None:
    """Attach start, settings, callback, and video-processing handlers."""

    @app.on_message(filters.command("start") & filters.private)
    async def start_handler(_client: Client, message: Any) -> None:
        await message.reply_text(
            "Send me a short video and I’ll analyze its visible content and write a video-generation prompt.\n\n"
            "Use /settings to choose the prompt style, analysis detail, and output language."
        )

    @app.on_message(filters.command("help") & filters.private)
    async def help_handler(_client: Client, message: Any) -> None:
        await message.reply_text(
            "Send a video as a video or document. I sample frames, separate visible observations from qualified "
            "inferences, and return a generation prompt. Use /settings to change your preferences."
        )

    @app.on_message(filters.command("settings") & filters.private)
    async def settings_handler(_client: Client, message: Any) -> None:
        if message.from_user is None:
            await message.reply_text("I could not identify your Telegram account.")
            return
        preferences = await store.get(message.from_user.id)
        await message.reply_text(_settings_summary(preferences), reply_markup=_overview_keyboard(preferences))

    @app.on_callback_query()
    async def settings_callback(_client: Client, query: Any) -> None:
        if query.from_user is None or query.message is None:
            await app.answer_callback_query(query.id)
            return
        await app.answer_callback_query(query.id)
        user_id = query.from_user.id
        data = query.data if isinstance(query.data, str) else ""
        preferences = await store.get(user_id)
        if data == "settings:done":
            try:
                await query.message.edit_text("Settings are ready. Send me a video whenever you like.")
            except Exception:
                logger.debug("Settings message was no longer editable", exc_info=True)
            return
        if data.startswith("settings:"):
            category = data.partition(":")[2]
            if category == "open":
                await query.message.edit_text(_settings_summary(preferences), reply_markup=_overview_keyboard(preferences))
                return
            if category in _SETTING_FIELDS:
                _key, label, _options = _SETTING_FIELDS[category]
                await query.message.edit_text(
                    f"{label}\nCurrent: {getattr(preferences, _key)}",
                    reply_markup=_choice_keyboard(category, preferences),
                )
                return
        if data.startswith("set:"):
            parts = data.split(":", 2)
            if len(parts) == 3:
                key, value = parts[1], parts[2]
                try:
                    preferences = await store.set(user_id, key, value)
                except ValueError:
                    await query.message.edit_text(
                        "That setting is not available. Please choose from the current options.",
                        reply_markup=_overview_keyboard(preferences),
                    )
                    return
                await query.message.edit_text(
                    _settings_summary(preferences, saved=True), reply_markup=_overview_keyboard(preferences)
                )

    @app.on_message((filters.video | filters.document) & filters.private)
    async def video_handler(client: Client, message: Any) -> None:
        user_id = message.from_user.id if message.from_user is not None else None
        if user_id is None:
            await message.reply_text("I could not identify your Telegram account.")
            return
        filename, mime_type, file_size = _media_attributes(message)
        max_bytes = config.max_video_size_mb * 1024 * 1024
        try:
            validate_video_upload(filename, mime_type, file_size, max_bytes)
        except VideoProcessingError as exc:
            await message.reply_text(str(exc))
            return

        status = await message.reply_text("Video received. Downloading and inspecting it…")
        logger.info("Video received from user_id=%s", user_id)
        temp_dir: Path | None = None
        try:
            temp_dir = Path(tempfile.mkdtemp(prefix="video-prompt-"))
            suffix = Path(filename or "video.mp4").suffix.lower()
            if suffix not in {".3gp", ".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm"}:
                suffix = ".video"
            target = temp_dir / f"source{suffix}"
            last_progress_update = 0.0
            next_progress_percent = 10

            async def update_download_progress(current: int, total: int) -> None:
                nonlocal last_progress_update, next_progress_percent
                if total <= 0:
                    return
                percent = min(100, current * 100 // total)
                now = time.monotonic()
                if current < total and (percent < next_progress_percent or now - last_progress_update < 5):
                    return
                try:
                    await status.edit_text(f"Downloading video… {percent}%")
                except Exception:
                    logger.debug("Could not update download progress message", exc_info=True)
                last_progress_update = now
                next_progress_percent = min(100, percent + 10)

            video_path = await download_telegram_video(
                client,
                message,
                target,
                timeout_seconds=config.download_timeout_seconds,
                progress_callback=update_download_progress,
            )
            logger.info("Download completed")
            if not video_path.is_file():
                raise VideoProcessingError("The downloaded video file could not be found.")
            local_size = video_path.stat().st_size
            validate_video_upload(filename, mime_type, local_size, max_bytes)
            logger.info("Downloaded video size: %d bytes", local_size)

            metadata = await probe_video(video_path, config.ffprobe_bin)
            logger.info("Video duration: %.1f seconds", metadata.duration_seconds)
            if metadata.duration_seconds > config.max_video_duration_seconds:
                raise VideoProcessingError(
                    f"The video is longer than this bot's {config.max_video_duration_seconds} second limit."
                )
            timestamps = generate_frame_timestamps(metadata.duration_seconds, config.frame_count)
            await status.edit_text(f"Video received. Extracting {len(timestamps)} frames for analysis…")
            frame_paths = await extract_frames(
                video_path,
                temp_dir / "frames",
                metadata.duration_seconds,
                frame_count=config.frame_count,
                ffmpeg_bin=config.ffmpeg_bin,
            )
            logger.info("Extracted %d frames", len(frame_paths))
            preferences = await store.get(user_id)
            analysis = await analyze_video_frames(
                ai_client, frame_paths, timestamps, preferences, config.openai_model
            )
            logger.info("AI analysis completed")
            response = format_analysis_response(analysis, preferences)
            logger.info("Prompt generated")
            await _send_text(client, message.chat.id, response, reply_to_message_id=message.id)
            try:
                await status.delete()
            except Exception:
                logger.debug("Could not remove completed status message", exc_info=True)
        except VideoProcessingError as exc:
            await status.edit_text(str(exc))
        except OpenAIError:
            logger.exception("OpenAI request failed")
            await status.edit_text("AI analysis failed. Please try again later.")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected video processing failure")
            await status.edit_text("I could not process this video. Please try another valid video file.")
        finally:
            if temp_dir is not None:
                try:
                    await asyncio.to_thread(cleanup_temp_path, temp_dir)
                    logger.info("Temporary files cleaned")
                except OSError:
                    logger.exception("Temporary files could not be cleaned")
