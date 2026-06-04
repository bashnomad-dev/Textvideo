"""Telegram-бот для транскрибации аудио, видео и ссылок в текст."""

import asyncio
import html
import logging
import os
import re
import sys

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode

from config import BOT_TOKEN, TEMP_DIR, MAX_FILE_SIZE_MB, FFMPEG_TIMEOUT, HEALTHCHECK_URL, HEALTHCHECK_INTERVAL
from transcriber import transcribe
from summarizer import summarize
from downloader import extract_url, fetch_youtube_transcript, fetch_subtitles, download_audio, cleanup, _extract_youtube_id, TranscriptPending
from cloud_downloader import detect_cloud_kind, extract_audio_from_url
from rate_limit import check_rate_limit, seconds_until_reset

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

TOO_BIG_MSG = (
    f"📦 Файл больше {MAX_FILE_SIZE_MB} МБ — Telegram не даёт ботам скачивать такие напрямую.\n\n"
    "Залей его на Google Drive, Яндекс.Диск или Dropbox (или дай прямую ссылку на файл) "
    "и пришли ссылку сюда — размер не ограничен, я сам вытащу аудио."
)


# --- Helpers ---

async def download_tg_file(file_id: str, ext: str = "ogg") -> str:
    """Скачивает файл из Telegram и возвращает путь."""
    os.makedirs(TEMP_DIR, exist_ok=True)
    file = await bot.get_file(file_id)
    local_path = os.path.join(TEMP_DIR, f"{file_id[:16]}.{ext}")
    await bot.download_file(file.file_path, local_path)
    return local_path


async def convert_to_mp3(input_path: str) -> str:
    """Конвертирует аудио/видео в mp3 через ffmpeg."""
    output_path = input_path.rsplit(".", 1)[0] + ".mp3"
    if input_path.endswith(".mp3"):
        return input_path
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", input_path, "-vn", "-acodec", "libmp3lame",
        "-q:a", "5", "-y", output_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=FFMPEG_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        cleanup(input_path)
        raise ValueError(f"Конвертация превысила таймаут ({FFMPEG_TIMEOUT} сек)")
    cleanup(input_path)
    return output_path


def split_text(text: str, max_len: int = 4000) -> list[str]:
    """Разбивает длинный текст на части для Telegram."""
    if len(text) <= max_len:
        return [text]
    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        # Ищем последний перенос строки или пробел
        cut = text.rfind("\n", 0, max_len)
        if cut == -1:
            cut = text.rfind(" ", 0, max_len)
        if cut == -1:
            cut = max_len
        parts.append(text[:cut])
        text = text[cut:].lstrip()
    return parts


def _safe_filename(s: str) -> str:
    """Готовит имя файла из заголовка."""
    cleaned = re.sub(r"[^\w\s\-]+", "", s or "", flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80] or "transcript"


async def send_result(message: types.Message, transcript: str, title: str = ""):
    """Отправляет саммари (если доступно) в чат + полный транскрипт файлом .txt."""
    summary = await summarize(transcript, title)

    head = f"📝 <b>{html.escape(title)}</b>\n\n" if title else ""
    body = html.escape(summary if summary else transcript)
    for i, part in enumerate(split_text(head + body)):
        if i == 0:
            await message.reply(part, parse_mode=ParseMode.HTML)
        else:
            await message.answer(part, parse_mode=ParseMode.HTML)

    # Полный транскрипт — всегда отдельным файлом
    doc = types.BufferedInputFile(transcript.encode("utf-8"), filename=f"{_safe_filename(title)}.txt")
    await message.answer_document(doc, caption="Полный транскрипт")


async def _safe_delete(msg: types.Message):
    try:
        await msg.delete()
    except Exception:
        pass


async def handle_tg_media(message: types.Message, status_text: str, file_id: str, ext: str, title: str):
    """Скачивает файл из Telegram и транскрибирует, гарантированно убирая статус.

    Скачивание вынесено в try: если Telegram отдаёт ошибку (например, файл >20 МБ),
    статус-сообщение не зависает, а юзер получает понятную ошибку.
    """
    status = await message.reply(status_text)
    try:
        path = await download_tg_file(file_id, ext)
        await process_and_reply(message, path, title)
    except Exception as e:
        log.exception("Не удалось скачать файл из Telegram")
        await message.reply(f"Не удалось скачать файл: {e}")
    finally:
        await _safe_delete(status)


async def process_and_reply(message: types.Message, file_path: str, source: str = ""):
    """Транскрибирует файл и отправляет результат."""
    try:
        mp3_path = await convert_to_mp3(file_path)
        text = await transcribe(mp3_path)
        cleanup(mp3_path)

        if not text:
            await message.reply("Не удалось распознать речь в этом файле.")
            return

        await send_result(message, text, source)

    except Exception as e:
        log.exception("Ошибка транскрибации")
        await message.reply(f"Ошибка: {e}")
    finally:
        cleanup(file_path)


# --- Rate limit check ---

async def rate_limit_guard(message: types.Message) -> bool:
    """Проверяет rate limit. Возвращает True если можно продолжать."""
    if check_rate_limit(message.from_user.id):
        return True
    wait = seconds_until_reset(message.from_user.id)
    await message.reply(f"⏸ Слишком много запросов. Подожди {wait} сек.")
    return False


# --- Handlers ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Отправь мне:\n\n"
        "🎤 <b>Голосовое</b> или <b>видеосообщение</b>\n"
        "🎵 <b>Аудиофайл</b> (mp3, wav, ogg...)\n"
        "🎬 <b>Видеофайл</b>\n"
        "🔗 <b>Ссылку</b>:\n"
        "   • YouTube, Instagram, TikTok, VK, X…\n"
        "   • Google Drive, Яндекс.Диск, Dropbox\n"
        "   • Прямую ссылку на mp3/mp4/wav и т.п.\n\n"
        "Пришлю структурированное саммари и полный транскрипт файлом .txt.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(F.voice)
async def on_voice(message: types.Message):
    if not await rate_limit_guard(message):
        return
    await handle_tg_media(message, "⏳ Распознаю голосовое...", message.voice.file_id, "ogg", "Голосовое сообщение")


@dp.message(F.video_note)
async def on_video_note(message: types.Message):
    if not await rate_limit_guard(message):
        return
    await handle_tg_media(message, "⏳ Распознаю видеосообщение...", message.video_note.file_id, "mp4", "Видеосообщение")


@dp.message(F.audio)
async def on_audio(message: types.Message):
    if not await rate_limit_guard(message):
        return
    if message.audio.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await message.reply(TOO_BIG_MSG)
        return
    ext = (message.audio.file_name or "audio.mp3").rsplit(".", 1)[-1]
    title = message.audio.title or message.audio.file_name or "Аудио"
    await handle_tg_media(message, "⏳ Распознаю аудио...", message.audio.file_id, ext, title)


@dp.message(F.video)
async def on_video(message: types.Message):
    if not await rate_limit_guard(message):
        return
    if message.video.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await message.reply(TOO_BIG_MSG)
        return
    await handle_tg_media(message, "⏳ Распознаю видео...", message.video.file_id, "mp4", "Видео")


@dp.message(F.document)
async def on_document(message: types.Message):
    if not await rate_limit_guard(message):
        return
    doc = message.document
    name = (doc.file_name or "").lower()
    audio_exts = (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".wma", ".opus")
    video_exts = (".mp4", ".mkv", ".avi", ".mov", ".webm")

    if not any(name.endswith(e) for e in audio_exts + video_exts):
        await message.reply("Отправь аудио/видео файл или ссылку.")
        return

    if doc.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await message.reply(TOO_BIG_MSG)
        return

    ext = name.rsplit(".", 1)[-1]
    await handle_tg_media(message, "⏳ Распознаю файл...", doc.file_id, ext, doc.file_name or "Файл")


@dp.message(F.text)
async def on_text(message: types.Message):
    if not await rate_limit_guard(message):
        return
    url = extract_url(message.text)
    if not url:
        await message.answer(
            "Отправь аудио, видео, голосовое или ссылку — я переведу в текст."
        )
        return

    is_youtube = _extract_youtube_id(url) is not None
    cloud_kind = None if is_youtube else detect_cloud_kind(url)
    status = await message.reply("⏳ Ищу субтитры...")
    try:
        if cloud_kind:
            label = {
                "gdrive": "Google Drive",
                "yadisk": "Яндекс.Диск",
                "dropbox": "Dropbox",
                "direct": "прямая ссылка",
            }.get(cloud_kind, "облако")
            await status.edit_text(f"⏳ Извлекаю аудио ({label})...")
            mp3_path, title, _ = await extract_audio_from_url(url, cloud_kind)
            try:
                text = await transcribe(mp3_path)
            finally:
                cleanup(mp3_path)
            if not text:
                await message.reply("Не удалось распознать речь в этом файле.")
                return
            await send_result(message, text, title)
            return

        if is_youtube:
            # YouTube — через Supadata. Длинные видео идут в async-обработку,
            # тогда меняем статус, чтобы не висело «Ищу субтитры» молча.
            async def _on_async():
                try:
                    await status.edit_text("⏳ Видео длинное, готовлю субтитры — до пары минут…")
                except Exception:
                    pass

            result = await fetch_youtube_transcript(url, on_async=_on_async)
            if result:
                text, title = result
                await send_result(message, text, title)
                await status.delete()
                return
            else:
                await message.reply("Не удалось получить субтитры для этого видео. Возможно, у него нет субтитров.")
                await status.delete()
                return

        # Не-YouTube: пробуем yt-dlp субтитры, потом скачивание аудио
        result = await fetch_subtitles(url)
        if result:
            text, title = result
            await send_result(message, text, title)
            await status.delete()
            return

        # Субтитров нет — скачиваем аудио и транскрибируем
        await status.edit_text("⏳ Субтитров нет, скачиваю аудио...")
        audio_path, title = await download_audio(url)
        await process_and_reply(message, audio_path, title)
    except TranscriptPending:
        await message.reply(
            "⏳ Видео длинное, субтитры ещё готовятся на стороне сервиса. "
            "Пришли эту же ссылку ещё раз через 1–2 минуты — они уже будут готовы."
        )
    except ValueError as e:
        await message.reply(f"Ошибка: {e}")
    except Exception as e:
        log.exception("Ошибка обработки ссылки")
        await message.reply(f"Не удалось обработать ссылку: {e}")
    finally:
        try:
            await status.delete()
        except Exception:
            pass


async def _healthcheck_loop():
    """Dead-man switch: пингуем Healthchecks.io, пока живы. Молчание = алерт юзеру."""
    if not HEALTHCHECK_URL:
        log.info("Healthcheck отключён (HEALTHCHECK_URL не задан)")
        return
    log.info("Healthcheck включён, пинг каждые %d сек", HEALTHCHECK_INTERVAL)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(HEALTHCHECK_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    await resp.read()
            except Exception as e:
                log.warning("Healthcheck ping не удался: %s", e)
            await asyncio.sleep(HEALTHCHECK_INTERVAL)


async def main():
    version = os.getenv("APP_VERSION", "unknown")
    log.info("Бот запущен (v%s)", version)
    os.makedirs(TEMP_DIR, exist_ok=True)
    asyncio.create_task(_healthcheck_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
