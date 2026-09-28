"""Per-user prompt preferences and SQLite persistence."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

PROMPT_STYLES = ("Cinematic", "Commercial", "Documentary", "Anime")
ANALYSIS_DETAILS = ("Low", "Medium", "High")
OUTPUT_LANGUAGES = ("English", "Hindi", "Spanish", "French", "German", "Japanese")
_ALLOWED_VALUES = {
    "prompt_style": PROMPT_STYLES,
    "analysis_detail": ANALYSIS_DETAILS,
    "output_language": OUTPUT_LANGUAGES,
}


@dataclass(frozen=True, slots=True)
class UserPreferences:
    """Video prompt preferences with the product's required defaults."""

    prompt_style: str = "Cinematic"
    analysis_detail: str = "High"
    output_language: str = "English"

    def __post_init__(self) -> None:
        for key, options in _ALLOWED_VALUES.items():
            if getattr(self, key) not in options:
                raise ValueError(f"Invalid {key.replace('_', ' ')}: {getattr(self, key)}")

    def to_dict(self) -> dict[str, str]:
        """Return the persistable preference fields."""
        return asdict(self)


class SQLiteSettingsStore:
    """Small asynchronous facade around a SQLite JSON settings table."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.database_path, timeout=30)

    async def initialize(self) -> None:
        """Create the settings table if this is a first run."""
        await asyncio.to_thread(self._initialize)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS user_settings ("
                    "user_id INTEGER PRIMARY KEY, settings_json TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )

    async def get(self, user_id: int) -> UserPreferences:
        """Return saved preferences, or defaults for a new or corrupt record."""
        return await asyncio.to_thread(self._get, user_id)

    def _get(self, user_id: int) -> UserPreferences:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT settings_json FROM user_settings WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return UserPreferences()
        try:
            values = json.loads(row[0])
            return UserPreferences(**values)
        except (TypeError, ValueError, json.JSONDecodeError):
            return UserPreferences()

    async def set(self, user_id: int, key: str, value: str) -> UserPreferences:
        """Update one validated preference and persist the resulting JSON record."""
        if key not in _ALLOWED_VALUES:
            raise ValueError(f"Unknown preference: {key}")
        if value not in _ALLOWED_VALUES[key]:
            raise ValueError(f"Invalid value for {key}: {value}")
        return await asyncio.to_thread(self._set, user_id, key, value)

    def _set(self, user_id: int, key: str, value: str) -> UserPreferences:
        current = self._get(user_id).to_dict()
        current[key] = value
        encoded = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO user_settings (user_id, settings_json) VALUES (?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET settings_json = excluded.settings_json, "
                    "updated_at = CURRENT_TIMESTAMP",
                    (user_id, encoded),
                )
        return UserPreferences(**current)
