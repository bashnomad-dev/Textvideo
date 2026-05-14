FROM python:3.12-slim

ARG CACHE_BUST=20260514v4
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg nodejs \
    && pip install --no-cache-dir yt-dlp \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV APP_VERSION=4
CMD ["python", "-u", "bot.py"]
