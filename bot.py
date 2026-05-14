"""Telegram-бот для транскрибации аудио, видео и ссылок в текст."""

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode

from config import BOT_TOKEN, TEMP_DIR, MAX_FILE_SIZE_MB, FFMPEG_TIMEOUT
from transcriber import transcribe
from downloader import extract_url, fetch_youtube_transcript, fetch_subtitles, download_audio, cleanup, _extract_youtube_id
from rate_limit import check_rate_limit, seconds_until_reset

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


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


async def process_and_reply(message: types.Message, file_path: str, source: str = ""):
    """Транскрибирует файл и отправляет результат."""
    try:
        mp3_path = await convert_to_mp3(file_path)
        text = await transcribe(mp3_path)
        cleanup(mp3_path)

        if not text:
            await message.reply("Не удалось распознать речь в этом файле.")
            return

        header = f"📝 <b>{source}</b>\n\n" if source else ""
        parts = split_text(header + text)
        for i, part in enumerate(parts):
            if i == 0:
                await message.reply(part, parse_mode=ParseMode.HTML)
            else:
                await message.answer(part)

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
        "🔗 <b>Ссылку</b> (YouTube, Instagram, TikTok...)\n\n"
        "Я переведу всё в текст.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(F.voice)
async def on_voice(message: types.Message):
    if not await rate_limit_guard(message):
        return
    status = await message.reply("⏳ Распознаю голосовое...")
    path = await download_tg_file(message.voice.file_id, "ogg")
    await process_and_reply(message, path, "Голосовое сообщение")
    await status.delete()


@dp.message(F.video_note)
async def on_video_note(message: types.Message):
    if not await rate_limit_guard(message):
        return
    status = await message.reply("⏳ Распознаю видеосообщение...")
    path = await download_tg_file(message.video_note.file_id, "mp4")
    await process_and_reply(message, path, "Видеосообщение")
    await status.delete()


@dp.message(F.audio)
async def on_audio(message: types.Message):
    if not await rate_limit_guard(message):
        return
    if message.audio.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await message.reply(f"Файл слишком большой (макс {MAX_FILE_SIZE_MB} МБ).")
        return
    status = await message.reply("⏳ Распознаю аудио...")
    ext = (message.audio.file_name or "audio.mp3").rsplit(".", 1)[-1]
    path = await download_tg_file(message.audio.file_id, ext)
    title = message.audio.title or message.audio.file_name or "Аудио"
    await process_and_reply(message, path, title)
    await status.delete()


@dp.message(F.video)
async def on_video(message: types.Message):
    if not await rate_limit_guard(message):
        return
    if message.video.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await message.reply(f"Файл слишком большой (макс {MAX_FILE_SIZE_MB} МБ).")
        return
    status = await message.reply("⏳ Распознаю видео...")
    path = await download_tg_file(message.video.file_id, "mp4")
    await process_and_reply(message, path, "Видео")
    await status.delete()


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
        await message.reply(f"Файл слишком большой (макс {MAX_FILE_SIZE_MB} МБ).")
        return

    status = await message.reply("⏳ Распознаю файл...")
    ext = name.rsplit(".", 1)[-1]
    path = await download_tg_file(doc.file_id, ext)
    await process_and_reply(message, path, doc.file_name or "Файл")
    await status.delete()


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
    status = await message.reply("⏳ Ищу субтитры...")
    try:
        if is_youtube:
            # YouTube — только через youtube-transcript-api (yt-dlp заблокирован)
            result = await fetch_youtube_transcript(url)
            if result:
                text, title = result
                header = f"📝 <b>{title}</b>\n\n"
                parts = split_text(header + text)
                for i, part in enumerate(parts):
                    if i == 0:
                        await message.reply(part, parse_mode=ParseMode.HTML)
                    else:
                        await message.answer(part)
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
            header = f"📝 <b>{title}</b>\n\n"
            parts = split_text(header + text)
            for i, part in enumerate(parts):
                if i == 0:
                    await message.reply(part, parse_mode=ParseMode.HTML)
                else:
                    await message.answer(part)
            await status.delete()
            return

        # Субтитров нет — скачиваем аудио и транскрибируем
        await status.edit_text("⏳ Субтитров нет, скачиваю аудио...")
        audio_path, title = await download_audio(url)
        await process_and_reply(message, audio_path, title)
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


async def main():
    version = os.getenv("APP_VERSION", "unknown")
    log.info("Бот запущен (v%s)", version)
    os.makedirs(TEMP_DIR, exist_ok=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
