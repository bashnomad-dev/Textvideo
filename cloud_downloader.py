"""Извлечение аудио из ссылок на облачные хранилища и прямых URL.

Источник стримится ffmpeg-ом прямо из HTTP в mp3 — большой видеофайл
не приземляется на диск целиком (1.5 ГБ MOV → ~30 МБ mp3).

Поддерживаются: Google Drive, Яндекс.Диск (disk.yandex / yadi.sk), Dropbox,
прямые ссылки на медиа-файлы.
"""

import asyncio
import logging
import os
import re
import uuid
from urllib.parse import unquote, urlparse

import aiohttp

from config import TEMP_DIR

log = logging.getLogger(__name__)

MEDIA_EXTS = (
    ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".wma", ".opus",
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".mpeg", ".mpg", ".3gp",
)

GDRIVE_ID = re.compile(r"(?:/file/d/|[?&]id=)([a-zA-Z0-9_-]{20,})")
YADISK_HOSTS = ("disk.yandex.", "yadi.sk")

# Жёсткий потолок на исходник — выше шёл бы транскрибат на десятки часов.
MAX_SOURCE_GB = float(os.getenv("MAX_SOURCE_GB", "5"))
# Таймаут на streaming-конвертацию (сек). 1.5 ГБ MOV → mp3 укладывается ~5-10 минут.
EXTRACT_TIMEOUT = int(os.getenv("EXTRACT_TIMEOUT", "1800"))


def detect_cloud_kind(url: str) -> str | None:
    """'gdrive' | 'yadisk' | 'dropbox' | 'direct' | None."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    if "drive.google.com" in host or "docs.google.com" in host:
        return "gdrive"
    if any(h in host for h in YADISK_HOSTS):
        return "yadisk"
    if "dropbox.com" in host:
        return "dropbox"

    if any(parsed.path.lower().endswith(e) for e in MEDIA_EXTS):
        return "direct"
    return None


async def _resolve_yadisk(url: str) -> tuple[str, str, int | None]:
    api = "https://cloud-api.yandex.net/v1/disk/public/resources"
    timeout = aiohttp.ClientTimeout(total=20)
    name = "Yandex Disk"
    size: int | None = None

    async with aiohttp.ClientSession() as session:
        async with session.get(api, params={"public_key": url}, timeout=timeout) as r:
            if r.status == 200:
                meta = await r.json()
                name = meta.get("name") or name
                size = meta.get("size")
            else:
                raise ValueError(f"Яндекс.Диск API HTTP {r.status} на метаданных")

        async with session.get(api + "/download", params={"public_key": url}, timeout=timeout) as r:
            if r.status != 200:
                raise ValueError(f"Яндекс.Диск API HTTP {r.status} на download")
            data = await r.json()

    href = data.get("href")
    if not href:
        raise ValueError("Яндекс.Диск не вернул прямую ссылку")
    return href, name, size


async def _resolve_gdrive(url: str) -> tuple[str, str, int | None]:
    m = GDRIVE_ID.search(url)
    if not m:
        raise ValueError("Не удалось извлечь ID файла Google Drive из ссылки")
    fid = m.group(1)
    direct = (
        f"https://drive.usercontent.google.com/download"
        f"?id={fid}&export=download&confirm=t"
    )
    return direct, f"gdrive_{fid}", None


async def _resolve_dropbox(url: str) -> tuple[str, str, int | None]:
    direct = re.sub(r"([?&])dl=0(\b|$)", r"\1dl=1", url)
    if "dl=" not in direct:
        sep = "&" if "?" in direct else "?"
        direct = f"{direct}{sep}dl=1"
    name = os.path.basename(urlparse(url).path) or "Dropbox"
    return direct, unquote(name), None


async def _resolve_direct(url: str) -> tuple[str, str, int | None]:
    name = os.path.basename(urlparse(url).path) or "Файл"
    return url, unquote(name), None


async def _resolve(url: str, kind: str) -> tuple[str, str, int | None]:
    if kind == "yadisk":
        return await _resolve_yadisk(url)
    if kind == "gdrive":
        return await _resolve_gdrive(url)
    if kind == "dropbox":
        return await _resolve_dropbox(url)
    if kind == "direct":
        return await _resolve_direct(url)
    raise ValueError(f"Неизвестный тип источника: {kind}")


async def extract_audio_from_url(url: str, kind: str) -> tuple[str, str, int | None]:
    """Стримит источник в ffmpeg, возвращает (mp3_path, title, size_bytes_or_None)."""
    direct, name, size = await _resolve(url, kind)

    if size and size > MAX_SOURCE_GB * 1024**3:
        raise ValueError(
            f"Файл слишком большой: {size / 1024**3:.1f} ГБ "
            f"(макс {MAX_SOURCE_GB:g} ГБ)"
        )

    os.makedirs(TEMP_DIR, exist_ok=True)
    mp3_path = os.path.join(TEMP_DIR, f"{uuid.uuid4().hex[:8]}.mp3")

    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10",
        "-i", direct,
        "-vn",
        "-acodec", "libmp3lame",
        "-q:a", "5",
        "-y", mp3_path,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=EXTRACT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        if os.path.exists(mp3_path):
            os.remove(mp3_path)
        raise ValueError(f"Извлечение аудио превысило таймаут ({EXTRACT_TIMEOUT} сек)")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        if os.path.exists(mp3_path):
            os.remove(mp3_path)
        hint = err.splitlines()[-1] if err else "ffmpeg вернул ошибку без сообщения"
        raise ValueError(f"ffmpeg не смог обработать источник: {hint[:300]}")

    if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) == 0:
        raise ValueError("Аудио не извлечено (пустой файл)")

    title = os.path.splitext(name)[0] or name
    return mp3_path, title, size
