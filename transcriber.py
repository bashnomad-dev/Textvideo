"""Модуль транскрибации аудио через Groq или OpenAI Whisper API."""

import os
from openai import AsyncOpenAI
from config import GROQ_API_KEY, OPENAI_API_KEY, TRANSCRIBE_PROVIDER


def _get_client() -> AsyncOpenAI:
    if TRANSCRIBE_PROVIDER == "groq":
        return AsyncOpenAI(
            api_key=GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1",
        )
    return AsyncOpenAI(api_key=OPENAI_API_KEY)


def _get_model() -> str:
    if TRANSCRIBE_PROVIDER == "groq":
        return "whisper-large-v3-turbo"
    return "whisper-1"


async def transcribe(file_path: str, language: str = "ru") -> str:
    """Транскрибирует аудиофайл в текст.

    Args:
        file_path: путь к аудиофайлу (mp3, wav, ogg, m4a, webm)
        language: язык аудио (ISO 639-1)

    Returns:
        Текст транскрибации
    """
    client = _get_client()
    model = _get_model()

    with open(file_path, "rb") as audio_file:
        response = await client.audio.transcriptions.create(
            model=model,
            file=audio_file,
            language=language,
            response_format="text",
        )

    return response.strip() if isinstance(response, str) else response.text.strip()
