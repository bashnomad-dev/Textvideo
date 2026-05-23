"""Скачивание аудио/видео из облачных хранилищ и по прямым ссылкам.

Поддерживаются: Google Drive, Яндекс.Диск (disk.yandex / yadi.sk), Dropbox,
прямые ссылки на медиа-файлы.
"""

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

# Лимит размера исходного файла, чтобы не залить /tmp. Аудио потом всё равно ужмётся ffmpeg.
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "500"))


def detect_cloud_kind(url: str) -> str | None:
    """Определяет тип источника. Возвращает 'gdrive' | 'yadisk' | 'dropbox' | 'direct' | None."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    if "drive.google.com" in host or "docs.google.com" in host:
        return "gdrive"
    if any(h in host for h in YADISK_HOSTS):
        return "yadisk"
    if "dropbox.com" in host:
        return "dropbox"

    path = parsed.path.lower()
    if any(path.endswith(e) for e in MEDIA_EXTS):
        return "direct"
    return None


def _filename_from_disposition(header: str) -> str | None:
    if not header:
        return None
    m = re.search(r"filename\*=UTF-8''([^;]+)", header, flags=re.IGNORECASE)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename="?([^";]+)"?', header, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def _rename_with_ext(path: str, name: str) -> str:
    """Если у скачанного файла нет расширения, добавляем его из имени источника."""
    ext = os.path.splitext(name)[1].lower()
    if not ext:
        return path
    new_path = os.path.splitext(path)[0] + ext
    if new_path == path:
        return path
    os.rename(path, new_path)
    return new_path


async def _stream(session: aiohttp.ClientSession, url: str, dest: str) -> tuple[str | None, str]:
    """Качает файл в dest. Возвращает (suggested_filename, content_type)."""
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
    async with session.get(url, allow_redirects=True, timeout=timeout) as resp:
        if resp.status != 200:
            raise ValueError(f"HTTP {resp.status} при скачивании файла")

        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text/html" in ctype:
            raise ValueError(
                "Сервер вернул HTML вместо файла. Возможно, ссылка не публичная "
                "или требует подтверждения скачивания."
            )

        clen = resp.headers.get("Content-Length")
        if clen and int(clen) > MAX_DOWNLOAD_MB * 1024 * 1024:
            raise ValueError(
                f"Файл слишком большой: {int(clen) / 1024 / 1024:.1f} МБ "
                f"(макс {MAX_DOWNLOAD_MB} МБ)"
            )

        suggested = _filename_from_disposition(resp.headers.get("Content-Disposition", ""))

        max_bytes = MAX_DOWNLOAD_MB * 1024 * 1024
        written = 0
        with open(dest, "wb") as f:
            async for chunk in resp.content.iter_chunked(1 << 16):
                written += len(chunk)
                if written > max_bytes:
                    f.close()
                    os.remove(dest)
                    raise ValueError(
                        f"Файл превысил {MAX_DOWNLOAD_MB} МБ во время скачивания"
                    )
                f.write(chunk)

        return suggested, ctype


async def _download_gdrive(url: str) -> tuple[str, str]:
    m = GDRIVE_ID.search(url)
    if not m:
        raise ValueError("Не удалось извлечь ID файла Google Drive из ссылки")
    file_id = m.group(1)
    direct = (
        f"https://drive.usercontent.google.com/download"
        f"?id={file_id}&export=download&confirm=t"
    )

    dest = os.path.join(TEMP_DIR, f"{uuid.uuid4().hex[:8]}.bin")
    async with aiohttp.ClientSession() as session:
        suggested, _ = await _stream(session, direct, dest)

    name = suggested or f"gdrive_{file_id}"
    title = os.path.splitext(name)[0] or "Файл Google Drive"
    return _rename_with_ext(dest, name), title


async def _download_yadisk(url: str) -> tuple[str, str]:
    api = "https://cloud-api.yandex.net/v1/disk/public/resources"
    async with aiohttp.ClientSession() as session:
        # Имя файла
        name = "Yandex Disk"
        async with session.get(
            api,
            params={"public_key": url},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            if r.status == 200:
                meta = await r.json()
                name = meta.get("name") or name

        # Прямая ссылка
        async with session.get(
            api + "/download",
            params={"public_key": url},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            if r.status != 200:
                raise ValueError(f"Яндекс.Диск API вернул HTTP {r.status}")
            data = await r.json()

        href = data.get("href")
        if not href:
            raise ValueError("Яндекс.Диск не вернул прямую ссылку на скачивание")

        dest = os.path.join(TEMP_DIR, f"{uuid.uuid4().hex[:8]}.bin")
        suggested, _ = await _stream(session, href, dest)

    final_name = suggested or name
    title = os.path.splitext(final_name)[0] or final_name
    return _rename_with_ext(dest, final_name), title


async def _download_dropbox(url: str) -> tuple[str, str]:
    direct = re.sub(r"([?&])dl=0", r"\1dl=1", url)
    if "dl=" not in direct:
        sep = "&" if "?" in direct else "?"
        direct = f"{direct}{sep}dl=1"

    dest = os.path.join(TEMP_DIR, f"{uuid.uuid4().hex[:8]}.bin")
    async with aiohttp.ClientSession() as session:
        suggested, _ = await _stream(session, direct, dest)

    name = suggested or os.path.basename(urlparse(url).path) or "Dropbox"
    title = os.path.splitext(name)[0] or name
    return _rename_with_ext(dest, name), title


async def _download_direct(url: str) -> tuple[str, str]:
    dest = os.path.join(TEMP_DIR, f"{uuid.uuid4().hex[:8]}.bin")
    async with aiohttp.ClientSession() as session:
        suggested, _ = await _stream(session, url, dest)

    name = suggested or os.path.basename(urlparse(url).path) or "Файл"
    name = unquote(name)
    title = os.path.splitext(name)[0] or name
    return _rename_with_ext(dest, name), title


async def download_cloud_file(url: str, kind: str) -> tuple[str, str]:
    """Скачивает файл по облачной/прямой ссылке. Возвращает (путь, название)."""
    os.makedirs(TEMP_DIR, exist_ok=True)
    if kind == "gdrive":
        return await _download_gdrive(url)
    if kind == "yadisk":
        return await _download_yadisk(url)
    if kind == "dropbox":
        return await _download_dropbox(url)
    if kind == "direct":
        return await _download_direct(url)
    raise ValueError(f"Неизвестный тип источника: {kind}")
