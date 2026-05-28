import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TRANSCRIBER_BOT_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TRANSCRIBE_PROVIDER = os.getenv("TRANSCRIBE_PROVIDER", "groq")  # groq | openai
SUPADATA_API_KEY = os.getenv("SUPADATA_API_KEY", "")
# Потолок прямого скачивания через Bot API — у Telegram это 20 МБ.
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "20"))
TEMP_DIR = "/tmp/textvideo"

# Саммаризация через OpenAI. Если OPENAI_API_KEY не задан — бот шлёт только транскрипт.
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "gpt-4o")
SUMMARY_MIN_CHARS = int(os.getenv("SUMMARY_MIN_CHARS", "400"))

# Rate limiting: макс запросов на юзера в минуту
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "10"))

# Retry: кол-во попыток и начальная задержка (сек)
RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "1.0"))

# Таймауты для внешних процессов (сек)
SUBPROCESS_TIMEOUT = int(os.getenv("SUBPROCESS_TIMEOUT", "120"))
FFMPEG_TIMEOUT = int(os.getenv("FFMPEG_TIMEOUT", "60"))
