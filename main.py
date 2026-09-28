"""Application entry point."""

from __future__ import annotations

import asyncio
import logging

from openai import AsyncOpenAI
from pyrogram import Client, idle
from pyrogram.enums import ParseMode

from video_prompt_bot.config import ConfigurationError, load_config
from video_prompt_bot.settings import SQLiteSettingsStore
from video_prompt_bot.telegram_ui import register_handlers


async def run() -> None:
    """Load services, start the Telegram client, and shut down cleanly."""
    config = load_config()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    workdir = config.database_path.parent.resolve()
    store = SQLiteSettingsStore(config.database_path)
    await store.initialize()
    ai_client = AsyncOpenAI(api_key=config.openai_api_key)
    app = Client(
        "video_prompt_bot",
        api_id=config.api_id,
        api_hash=config.api_hash,
        bot_token=config.bot_token,
        workdir=str(workdir),
        parse_mode=ParseMode.DISABLED,
    )
    register_handlers(app, config, store, ai_client)
    started = False
    try:
        await app.start()
        started = True
        logging.getLogger(__name__).info("Video prompt bot started")
        await idle()
    finally:
        try:
            if started:
                await app.stop()
        finally:
            await ai_client.close()


def main() -> None:
    """Start the bot and report configuration failures clearly."""
    try:
        asyncio.run(run())
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
