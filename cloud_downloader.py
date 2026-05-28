"""Извлечение аудио из ссылок на облачные хранилища и прямых URL.

Источник качается на диск через aria2c в несколько потоков (на порядок быстрее
одиночного HTTP-стрима и без двойного чтения MOV без faststart), затем локальный
ffmpeg вытаскивает дорожку в mono/16k mp3 (1.5 ГБ MOV → единицы МБ mp3).

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
# Таймаут на локальную ffmpeg-конвертацию уже скачанного файла (сек).
EXTRACT_TIMEOUT = int(os.getenv("EXTRACT_TIMEOUT", "1800"))
# Параллельная загрузка через aria2c.
ARIA_CONNECTIONS = int(os.getenv("ARIA_CONNECTIONS", "16"))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "1200"))
# Подсказка ffmpeg по контейнеру: расширение исходника, если оно есть в имени.
_EXT_RE = re.compile(r"\.([a-z0-9]{2,4})$", re.IGNORECASE)


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


async def _download(direct: str, dest: str) -> None:
    """Качает файл в несколько потоков через aria2c."""
    cmd = [
        "aria2c",
        "-x", str(ARIA_CONNECTIONS),
        "-s", str(ARIA_CONNECTIONS),
        "-k", "1M",
        "--max-tries=3", "--retry-wait=5",
        "--console-log-level=warn", "--summary-interval=0",
        "--allow-overwrite=true", "--auto-file-renaming=false",
        "-d", os.path.dirname(dest), "-o", os.path.basename(dest),
        direct,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        if os.path.exists(dest):
            os.remove(dest)
        raise ValueError(f"Скачивание превысило таймаут ({DOWNLOAD_TIMEOUT} сек)")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        if os.path.exists(dest):
            os.remove(dest)
        hint = err.splitlines()[-1] if err else "aria2c вернул ошибку без сообщения"
        raise ValueError(f"Не удалось скачать файл: {hint[:300]}")

    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise ValueError("Скачанный файл пуст")

    got = os.path.getsize(dest)
    if got > MAX_SOURCE_GB * 1024**3:
        os.remove(dest)
        raise ValueError(
            f"Файл слишком большой: {got / 1024**3:.1f} ГБ (макс {MAX_SOURCE_GB:g} ГБ)"
        )


async def _to_mp3(src: str, mp3_path: str) -> None:
    """Вытаскивает аудиодорожку из локального файла в mono/16k mp3."""
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k",
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


async def extract_audio_from_url(url: str, kind: str) -> tuple[str, str, int | None]:
    """Качает источник на диск и вытаскивает аудио. (mp3_path, title, size_bytes_or_None)."""
    direct, name, size = await _resolve(url, kind)

    if size and size > MAX_SOURCE_GB * 1024**3:
        raise ValueError(
            f"Файл слишком большой: {size / 1024**3:.1f} ГБ "
            f"(макс {MAX_SOURCE_GB:g} ГБ)"
        )

    os.makedirs(TEMP_DIR, exist_ok=True)
    token = uuid.uuid4().hex[:8]
    ext_m = _EXT_RE.search(name)
    src_path = os.path.join(TEMP_DIR, f"{token}.{ext_m.group(1) if ext_m else 'src'}")
    mp3_path = os.path.join(TEMP_DIR, f"{token}.mp3")

    try:
        await _download(direct, src_path)
        await _to_mp3(src_path, mp3_path)
    finally:
        if os.path.exists(src_path):
            os.remove(src_path)

    title = os.path.splitext(name)[0] or name
    return mp3_path, title, size
