FROM python:3.12-slim

ARG CACHE_BUST=20260528v7
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg aria2 curl ca-certificates unzip \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && deno --version \
    && pip install --no-cache-dir yt-dlp \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV APP_VERSION=7
CMD ["python", "-u", "bot.py"]
