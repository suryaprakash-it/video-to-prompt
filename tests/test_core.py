"""Dependency-free tests for core bot functions."""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from video_prompt_bot.config import ConfigurationError, load_config
from video_prompt_bot.prompt import VideoAnalysis, format_analysis_response, split_telegram_message
from video_prompt_bot.settings import SQLiteSettingsStore, UserPreferences
from video_prompt_bot.video import (
    VideoProcessingError,
    cleanup_temp_path,
    generate_frame_timestamps,
    parse_probe_output,
    validate_video_upload,
)


class VideoTests(unittest.TestCase):
    def test_video_metadata_extraction(self) -> None:
        metadata = parse_probe_output(json.dumps({
            "format": {"duration": "8.4", "size": "512000"},
            "streams": [{
                "codec_type": "video", "width": 1920, "height": 1080,
                "avg_frame_rate": "24000/1001",
            }],
        }))
        self.assertEqual(metadata.duration_seconds, 8.4)
        self.assertEqual((metadata.width, metadata.height), (1920, 1080))
        self.assertAlmostEqual(metadata.frame_rate or 0, 23.976, places=2)
        self.assertEqual(metadata.size_bytes, 512000)

    def test_frame_timestamp_generation(self) -> None:
        self.assertEqual(generate_frame_timestamps(10, 4), [2.0, 4.0, 6.0, 8.0])

    def test_invalid_metadata_and_video_uploads(self) -> None:
        with self.assertRaises(VideoProcessingError):
            parse_probe_output('{"streams": []}')
        with self.assertRaises(ValueError):
            generate_frame_timestamps(0, 4)
        with self.assertRaises(ValueError):
            generate_frame_timestamps(10, 0)
        with self.assertRaises(VideoProcessingError):
            validate_video_upload("notes.txt", "text/plain", 100, 1000)
        with self.assertRaises(VideoProcessingError):
            validate_video_upload("clip.mp4", "video/mp4", 1001, 1000)

    def test_temporary_file_and_directory_cleanup(self) -> None:
        directory = Path(".unit-test-cleanup")
        file_path = Path(".unit-test-temporary.txt")
        try:
            directory.mkdir(exist_ok=True)
            nested = directory / "nested.txt"
            nested.write_text("temporary", encoding="utf-8")
            cleanup_temp_path(directory)
            self.assertFalse(directory.exists())
            file_path.write_text("temporary", encoding="utf-8")
            cleanup_temp_path(file_path)
            cleanup_temp_path(file_path)
            self.assertFalse(file_path.exists())
        finally:
            cleanup_temp_path(directory)
            cleanup_temp_path(file_path)


class PromptTests(unittest.TestCase):
    def test_prompt_formatting_keeps_observed_and_inferred_separate(self) -> None:
        preferences = UserPreferences()
        output = format_analysis_response(
            VideoAnalysis(
                observed="A person walks beside a red car.",
                inferred="The setting may be an outdoor parking area.",
                video_prompt="A person walks beside a red car in a steady medium shot.",
            ),
            preferences,
        )
        self.assertIn("OBSERVED\nA person walks beside a red car.", output)
        self.assertIn("INFERRED\nThe setting may be an outdoor parking area.", output)
        self.assertIn("VIDEO GENERATION PROMPT\nA person walks beside a red car", output)
        self.assertIn("Cinematic · High detail · English", output)

    def test_telegram_message_splitting_obeys_limit(self) -> None:
        text = "Section one.\n\n" + "word 😀" * 20 + "\n\nSection three."
        chunks = split_telegram_message(text, max_length=32)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 32 for chunk in chunks))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.strip() for chunk in chunks))
        self.assertEqual("".join(chunks), text)

    def test_invalid_telegram_message_limit(self) -> None:
        with self.assertRaises(ValueError):
            split_telegram_message("text", 0)


class ConfigurationTests(unittest.TestCase):
    def test_configuration_loading_defaults_and_env_file_precedence(self) -> None:
        env_file = Path(".unit-test.env")
        try:
            env_file.write_text(
                "API_ID=123\nAPI_HASH=from-file\nBOT_TOKEN=token\nOPENAI_API_KEY=key\nFRAME_COUNT=12\n",
                encoding="utf-8",
            )
            config = load_config(env={"API_HASH": "from-process"}, env_file=env_file)
        finally:
            env_file.unlink(missing_ok=True)
        self.assertEqual(config.api_id, 123)
        self.assertEqual(config.api_hash, "from-process")
        self.assertEqual(config.frame_count, 12)
        self.assertEqual(config.max_video_size_mb, 100)

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_config(env={}, env_file=None)
        values = {"API_ID": "bad", "API_HASH": "hash", "BOT_TOKEN": "token", "OPENAI_API_KEY": "key"}
        with self.assertRaises(ConfigurationError):
            load_config(env=values, env_file=None)
        values["API_ID"] = "100"
        values["FRAME_COUNT"] = "25"
        with self.assertRaises(ConfigurationError):
            load_config(env=values, env_file=None)


class SettingsStoreTests(unittest.TestCase):
    def test_configuration_persists_across_store_instances(self) -> None:
        async def exercise(database: Path) -> None:
            first = SQLiteSettingsStore(database)
            await first.initialize()
            self.assertEqual(await first.get(42), UserPreferences())
            updated = await first.set(42, "prompt_style", "Documentary")
            self.assertEqual(updated.prompt_style, "Documentary")
            second = SQLiteSettingsStore(database)
            self.assertEqual((await second.get(42)).prompt_style, "Documentary")
            with self.assertRaises(ValueError):
                await second.set(42, "analysis_detail", "Extreme")

        database = Path(".unit-test-settings.sqlite3")
        try:
            asyncio.run(exercise(database))
        finally:
            database.unlink(missing_ok=True)
            Path(str(database) + "-wal").unlink(missing_ok=True)
            Path(str(database) + "-shm").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
