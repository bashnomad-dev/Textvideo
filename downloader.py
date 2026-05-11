"""Модуль скачивания аудио и субтитров из ссылок через yt-dlp."""

import asyncio
import json
import os
import re
import uuid
from config import TEMP_DIR, MAX_FILE_SIZE_MB

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


def extract_url(text: str) -> str | None:
    """Извлекает первую ссылку из текста."""
    match = URL_PATTERN.search(text)
    return match.group(0) if match else None


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
