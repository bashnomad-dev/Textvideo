# textvideo agent guide

Use this file to help AI agents work safely in the `textvideo` repo.

## What this repo is
- A Telegram bot for transcribing audio/video and writing summaries.
- Supports direct audio uploads, links to cloud files, and social media video URLs.
- Uses OpenAI / Groq for transcription and summarization depending on env configuration.
- Includes a Dockerfile, but local development is usually done in a Python venv.

## Key files
- `bot.py` — main Telegram bot logic and entrypoint.
- `config.py` — environment variables and service/provider settings.
- `transcriber.py` — speech-to-text integration.
- `summarizer.py` — summary generation.
- `downloader.py` — media downloading and URL parsing.
- `cloud_downloader.py` — cloud link extraction.
- `rate_limit.py` — per-user throttling.

## Run commands
- `python -m venv .venv && source .venv/bin/activate`
- `pip install -r requirements.txt`
- `python bot.py`
- Use Docker if you need containerized execution: `docker build -t textvideo .`

## Important conventions
- Do not commit secrets or `.env` values.
- `config.py` loads `.env`; the repo already has `.env.example`.
- The bot should keep temporary files under `TEMP_DIR` and clean up after processing.
- Prefer minimal changes to core transcription flow unless the feature explicitly requires it.

## What not to change without asking
- `.env` and any secret config values.
- The bot polling entrypoint in `bot.py` unless switching to a different deployment mode.
- External provider selection in `config.py` without verifying `TRANSCRIBE_PROVIDER` behavior.
