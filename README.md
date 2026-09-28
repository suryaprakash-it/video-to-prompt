# Video Prompt Telegram Bot

A Pyrogram bot that accepts a short video, downloads it to a temporary directory, reads metadata with FFprobe, extracts up to 24 evenly spaced frames with FFmpeg, and asks OpenAI's vision model for an evidence-grounded video-generation prompt. Each response separates **OBSERVED** details from qualified **INFERRED** details.

User preferences are stored in SQLite as a JSON settings value and survive bot restarts. The `/settings` menu controls prompt style, analysis detail, and output language.

## Project structure

```text
.
├── main.py
├── video_prompt_bot/
│   ├── __init__.py
│   ├── config.py
│   ├── prompt.py
│   ├── settings.py
│   ├── telegram_ui.py
│   └── video.py
├── tests/
│   ├── __init__.py
│   └── test_core.py
├── Dockerfile
├── docker-compose.yml
├── render.yaml
├── requirements.txt
├── .env.example
└── README.md
```

## Requirements

- Python 3.10 or later
- FFmpeg and FFprobe available on `PATH` (or set `FFMPEG_BIN` and `FFPROBE_BIN`)
- Telegram API credentials from [my.telegram.org](https://my.telegram.org) and a bot token from [@BotFather](https://t.me/BotFather)
- An OpenAI API key with access to a vision-capable chat completions model

## Install and configure

Create a virtual environment and install dependencies:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

On macOS or Linux, activate with `source .venv/bin/activate`. Install FFmpeg using the package manager for your operating system and confirm `ffmpeg -version` and `ffprobe -version` work.

Copy `.env.example` to `.env` and set these required values:

| Variable | Purpose |
| --- | --- |
| `API_ID` | Telegram application ID |
| `API_HASH` | Telegram application hash |
| `BOT_TOKEN` | BotFather token |
| `OPENAI_API_KEY` | Key used for visual analysis and prompt generation |

Optional values:

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI vision model name |
| `DATABASE_PATH` | `data/settings.sqlite3` | SQLite preferences database |
| `FFMPEG_BIN` | `ffmpeg` | FFmpeg executable name or path |
| `FFPROBE_BIN` | `ffprobe` | FFprobe executable name or path |
| `MAX_VIDEO_SIZE_MB` | `100` | Maximum accepted Telegram upload size |
| `MAX_VIDEO_DURATION_SECONDS` | `180` | Maximum accepted video duration |
| `FRAME_COUNT` | `24` | Number of evenly spaced frames (1–24) |
| `DOWNLOAD_TIMEOUT_SECONDS` | `300` | Maximum time allowed for the Telegram video download |

The bot's `.env` reader supports simple `KEY=VALUE` lines, comments, and quoted values. Existing process environment variables take precedence.

## Run locally

From the project root, activate the virtual environment, then run:

```powershell
python main.py
```

Run the unit suite separately with:

```powershell
python -m unittest discover -v
```

The bot does not run tests during startup.

## Run with Docker

Create `.env` as above, then build and start:

```sh
docker compose up --build -d
docker compose logs -f
```

The Compose configuration keeps SQLite data in a named `bot_data` volume. Stop the bot with `docker compose down`; the settings volume remains available for the next run.

## Example Telegram interaction

1. Send `/start` to the bot.
2. Send a short video (or a video document).
3. The bot replies with sections named `OBSERVED`, `INFERRED`, and `VIDEO GENERATION PROMPT`.
4. Send `/settings` and tap **Prompt Style**, **Analysis Detail**, or **Output Language**. Defaults are **Cinematic**, **High**, and **English**.

## Operational behavior and limitations

- Supported upload formats depend on FFmpeg; common MP4, MOV, MKV, AVI, WebM, MPEG, and 3GP files are accepted by the input check.
- Files over the configured size or duration limit are rejected before visual analysis. Up to 24 resized frames are sent to OpenAI.
- For Render, deploy the container as a **Background Worker**, not a Web Service. The bot makes no inbound HTTP listener; Render web services require a port, while background workers are intended for continuously running processes that make outbound requests. Attach a persistent disk at `/app/data` and set `DATABASE_PATH=/app/data/settings.sqlite3` to preserve settings across restarts.
- `render.yaml` is an optional Render Blueprint for a Docker Background Worker with a persistent data disk. Add the four secrets in the Render dashboard when prompted. Existing Web Services cannot be converted in place to a different service type; create this worker, then stop the web service so only one bot instance polls Telegram.
- Telegram and OpenAI credentials are required to run the live bot. Unit tests cover core behavior without contacting those services.
- The OpenAI key must belong to an API project with available quota. A valid key can still receive `429 insufficient_quota` when API credits are exhausted or the project's spending limit is reached; review API usage and billing before retrying.
- The vision model can miss fast motion, small text, or events between sampled frames. The prompt explicitly asks it to label observations separately from qualified inferences and omit unsupported camera or location claims.
- OpenAI usage may incur API charges. Review your account's current pricing and model availability before choosing a model.
