import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TRANSCRIBER_BOT_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TRANSCRIBE_PROVIDER = os.getenv("TRANSCRIBE_PROVIDER", "groq")  # groq | openai
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "25"))
TEMP_DIR = "/tmp/textvideo"
