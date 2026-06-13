# MAP — Textvideo

> Telegram-бот транскрибации видео/аудио/ссылок в текст. Прод на Railway.

## Что это
Бот принимает ссылку на YouTube/Instagram/TikTok/VK/Twitter или загруженный файл — возвращает текстовую транскрипцию.

## Хозяин
Ринат Тагиров (id 62090664)

## Статус
**prod** — работает на Railway, активно используется

## Стек
- Python 3.11+
- aiogram 3.15+ (Telegram Bot API)
- openai SDK — Whisper API для транскрипции аудио
- aiohttp — Supadata API (готовые YouTube-субтитры если есть)
- yt-dlp (через subprocess) — скачивание медиа
- ffmpeg (системно) — преобразование форматов
- Deploy: **Railway**, проект $RAILWAY_PROJECT_ID

## Пути
- Код: `~/projects/Textvideo/`
- Модули: `bot.py` (точка входа), `config.py`, `downloader.py`, `transcriber.py`, `rate_limit.py`
- Конфиг: `.env` (переменные окружения)
- Временные файлы: `TEMP_DIR` из config
- Dockerfile в корне

## Доступы / ключи (имена)
- `BOT_TOKEN` (или `TRANSCRIBER_BOT_TOKEN`) — Telegram-бот
- `OPENAI_API_KEY` — Whisper API
- `SUPADATA_API_KEY` — YouTube-транскрипты
- `TEMP_DIR`, `MAX_FILE_SIZE_MB`, `FFMPEG_TIMEOUT` — пути и лимиты

## Контакты
Внешних клиентов нет, продукт публичный.

## Активные процессы
- Railway: один service деплоится из main ветки
- `git push` в main → автодеплой
- Логи: Railway dashboard или `railway logs`

## Известные проблемы / открытые задачи
- На 12.06 в bot.py был незакоммиченный change от штаба (YouTube audio fallback) — статус неизвестен мне (не моя зона)
- Rate limiting на пользователя (rate_limit.py) — функционирует

## Кто что делал недавно
- 10-11.06: штаб добавил rate limiting, retry с backoff, таймауты процессов, переход на deno для yt-dlp JS runtime, Supadata API для YouTube-транскриптов
- Оптимус (default) — read-only зона, не модифицирует код

## Деплой
- `git push origin main` — Railway автоматически пересобирает и деплоит
- Откат: Railway dashboard → Deployments → Rollback

## Зона ответственности
**Штаб** ведёт код продукта. **Оптимус** — read-only, ошибки эскалирует штабу, не правит сам.
