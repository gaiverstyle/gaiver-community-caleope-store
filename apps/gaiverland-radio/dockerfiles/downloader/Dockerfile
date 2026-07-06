FROM python:3.12-slim
RUN apt-get update -qq && \
    apt-get install -y -qq ffmpeg && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
    httpx yt-dlp tzdata psycopg2-binary
