"""Модуль транскрибации аудио через Groq или OpenAI Whisper API."""

import asyncio
import glob
import logging
import os
import uuid

from openai import AsyncOpenAI

from config import (
    GROQ_API_KEY, OPENAI_API_KEY, TRANSCRIBE_PROVIDER,
    RETRY_MAX_ATTEMPTS, RETRY_BASE_DELAY, TEMP_DIR,
)

log = logging.getLogger(__name__)

# Whisper API (Groq/OpenAI) принимает файл до 25 МБ. Держим запас.
CHUNK_THRESHOLD_MB = float(os.getenv("TRANSCRIBE_CHUNK_MB", "24"))
# Длина куска при нарезке. При mono/16kHz/64kbps это ~4.8 МБ за 10 минут.
CHUNK_SECONDS = int(os.getenv("TRANSCRIBE_CHUNK_SECONDS", "600"))
CHUNK_TIMEOUT = int(os.getenv("TRANSCRIBE_CHUNK_TIMEOUT", "600"))


def _get_client() -> AsyncOpenAI:
    if TRANSCRIBE_PROVIDER == "groq":
        return AsyncOpenAI(
            api_key=GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1",
        )
    return AsyncOpenAI(api_key=OPENAI_API_KEY)


def _get_model() -> str:
    if TRANSCRIBE_PROVIDER == "groq":
        return "whisper-large-v3-turbo"
    return "whisper-1"


async def _split_audio(file_path: str) -> list[str]:
    """Режет аудио на mono/16kHz mp3-куски заведомо меньше лимита API."""
    os.makedirs(TEMP_DIR, exist_ok=True)
    base = os.path.join(TEMP_DIR, f"chunk_{uuid.uuid4().hex[:8]}")
    pattern = f"{base}_%03d.mp3"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", file_path,
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k",
        "-f", "segment", "-segment_time", str(CHUNK_SECONDS),
        "-reset_timestamps", "1",
        "-y", pattern,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=CHUNK_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise ValueError(f"Нарезка аудио превысила таймаут ({CHUNK_TIMEOUT} сек)")

    if proc.returncode != 0:
        hint = stderr.decode(errors="replace").strip()[-300:]
        raise ValueError(f"ffmpeg не смог нарезать аудио: {hint}")

    chunks = sorted(glob.glob(f"{base}_*.mp3"))
    if not chunks:
        raise ValueError("Нарезка аудио не дала ни одного файла")
    return chunks


async def _transcribe_one(file_path: str, language: str) -> str:
    """Одна попытка транскрибации файла с ретраями."""
    client = _get_client()
    model = _get_model()

    last_error = None
    for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
        try:
            with open(file_path, "rb") as audio_file:
                response = await client.audio.transcriptions.create(
                    model=model,
                    file=audio_file,
                    language=language,
                    response_format="text",
                )
            return response.strip() if isinstance(response, str) else response.text.strip()
        except Exception as e:
            last_error = e
            if attempt < RETRY_MAX_ATTEMPTS:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                log.warning("Whisper API attempt %d/%d failed: %s. Retry in %.1fs", attempt, RETRY_MAX_ATTEMPTS, e, delay)
                await asyncio.sleep(delay)
            else:
                log.error("Whisper API failed after %d attempts: %s", RETRY_MAX_ATTEMPTS, e)

    raise last_error


async def transcribe(file_path: str, language: str = "ru") -> str:
    """Транскрибирует аудиофайл в текст.

    Файлы больше лимита API режутся на куски, транскрибируются по очереди
    и склеиваются.

    Args:
        file_path: путь к аудиофайлу (mp3, wav, ogg, m4a, webm)
        language: язык аудио (ISO 639-1)

    Returns:
        Текст транскрибации
    """
    if os.path.getsize(file_path) <= CHUNK_THRESHOLD_MB * 1024 * 1024:
        return await _transcribe_one(file_path, language)

    chunks = await _split_audio(file_path)
    log.info("Файл %d МБ нарезан на %d кусков", os.path.getsize(file_path) // 1024**2, len(chunks))
    try:
        parts = []
        for i, chunk in enumerate(chunks, 1):
            log.info("Транскрибирую кусок %d/%d", i, len(chunks))
            parts.append(await _transcribe_one(chunk, language))
        return "\n".join(p for p in parts if p).strip()
    finally:
        for chunk in chunks:
            if os.path.exists(chunk):
                os.remove(chunk)
