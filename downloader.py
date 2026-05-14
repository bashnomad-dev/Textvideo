"""Модуль скачивания аудио и субтитров из ссылок через yt-dlp и youtube-transcript-api."""

import asyncio
import json
import logging
import os
import re
import uuid
from config import TEMP_DIR, MAX_FILE_SIZE_MB

log = logging.getLogger(__name__)

# Паттерн для определения ссылок
URL_PATTERN = re.compile(
    r'https?://(?:www\.)?'
    r'(?:youtube\.com/watch\?v=|youtu\.be/|'
    r'instagram\.com/(?:p|reel|reels)/|'
    r'tiktok\.com/|vm\.tiktok\.com/|'
    r'vk\.com/(?:video|clip)|'
    r'twitter\.com/|x\.com/|'
    r'reddit\.com/|'
    r'\S+\.\S+/\S+)'  # любая другая ссылка
    r'\S*'
)


YOUTUBE_ID_PATTERN = re.compile(
    r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})'
)


def extract_url(text: str) -> str | None:
    """Извлекает первую ссылку из текста."""
    match = URL_PATTERN.search(text)
    return match.group(0) if match else None


def _extract_youtube_id(url: str) -> str | None:
    """Извлекает YouTube video ID из URL."""
    match = YOUTUBE_ID_PATTERN.search(url)
    return match.group(1) if match else None


async def fetch_youtube_transcript(url: str, lang: str = "ru") -> tuple[str, str] | None:
    """Получает транскрипт YouTube-видео через youtube-transcript-api.

    Returns:
        (текст, название видео) или None
    """
    video_id = _extract_youtube_id(url)
    if not video_id:
        return None

    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        ytt_api = YouTubeTranscriptApi()
        transcript = ytt_api.fetch(video_id, languages=[lang, "en"])
        text = " ".join(s.text for s in transcript.snippets)

        if not text or len(text.strip()) < 20:
            return None

        title = await _get_youtube_title(video_id)
        return text, title

    except Exception as e:
        log.warning("youtube-transcript-api failed for %s: %s", video_id, e)
        return None


async def _get_youtube_title(video_id: str) -> str:
    """Получает название YouTube-видео через oembed API (без ключей и куков)."""
    import urllib.request
    import urllib.parse
    try:
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return data.get("title", "Без названия")
    except Exception:
        return "Без названия"


async def fetch_subtitles(url: str, lang: str = "ru") -> tuple[str, str] | None:
    """Пытается получить готовые субтитры из видео.

    Returns:
        (текст субтитров, название видео) или None если субтитров нет
    """
    os.makedirs(TEMP_DIR, exist_ok=True)
    file_id = uuid.uuid4().hex[:8]
    sub_path = os.path.join(TEMP_DIR, file_id)

    # Сначала получаем инфо о доступных субтитрах
    cmd = [
        "yt-dlp",
        "--js-runtimes", "node",
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", f"{lang},en",
        "--sub-format", "json3",
        "--no-playlist",
        "--print", "title",
        "-o", sub_path,
        url,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        return None

    title = stdout.decode().strip().split("\n")[0] or "Без названия"

    # Ищем скачанный файл субтитров
    sub_file = None
    for f in os.listdir(TEMP_DIR):
        if f.startswith(file_id) and f.endswith(".json3"):
            sub_file = os.path.join(TEMP_DIR, f)
            break

    if not sub_file or not os.path.exists(sub_file):
        return None

    try:
        text = _parse_json3_subs(sub_file)
        cleanup(sub_file)
        if text and len(text.strip()) > 20:
            return text, title
    except Exception:
        cleanup(sub_file)

    return None


def _parse_json3_subs(path: str) -> str:
    """Парсит json3 субтитры в чистый текст без дубликатов."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    segments = []
    prev_text = ""
    for event in data.get("events", []):
        parts = event.get("segs", [])
        line = "".join(s.get("utf8", "") for s in parts).strip()
        # json3 авто-субтитры часто дублируют строки
        if line and line != prev_text and line != "\n":
            segments.append(line)
            prev_text = line

    return " ".join(segments)


async def download_audio(url: str) -> tuple[str, str]:
    """Скачивает аудио из URL через yt-dlp.

    Returns:
        (путь к файлу, название видео)

    Raises:
        ValueError: если файл слишком большой или скачивание не удалось
    """
    os.makedirs(TEMP_DIR, exist_ok=True)
    file_id = uuid.uuid4().hex[:8]
    output_path = os.path.join(TEMP_DIR, f"{file_id}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--js-runtimes", "node",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "5",
        "--max-filesize", f"{MAX_FILE_SIZE_MB}m",
        "--no-playlist",
        "--print", "title",
        "-o", output_path,
        url,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        error = stderr.decode().strip()
        raise ValueError(f"Не удалось скачать: {error[:200]}")

    title = stdout.decode().strip().split("\n")[0] or "Без названия"

    # Найти скачанный файл
    actual_path = os.path.join(TEMP_DIR, f"{file_id}.mp3")
    if not os.path.exists(actual_path):
        for f in os.listdir(TEMP_DIR):
            if f.startswith(file_id):
                actual_path = os.path.join(TEMP_DIR, f)
                break
        else:
            raise ValueError("Файл не найден после скачивания")

    size_mb = os.path.getsize(actual_path) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        os.remove(actual_path)
        raise ValueError(f"Файл слишком большой: {size_mb:.1f} МБ (макс {MAX_FILE_SIZE_MB} МБ)")

    return actual_path, title


def cleanup(file_path: str):
    """Удаляет временный файл."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except OSError:
        pass
