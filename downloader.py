"""Модуль скачивания аудио из ссылок через yt-dlp."""

import asyncio
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
        "--audio-quality", "5",  # средне-низкое качество для экономии
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
        # yt-dlp мог сохранить с другим расширением
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
