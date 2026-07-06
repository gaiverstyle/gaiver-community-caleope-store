FROM python:3.12-slim
RUN apt-get update -qq && \
    apt-get install -y -qq ffmpeg && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
    fastapi "uvicorn[standard]" psycopg2-binary httpx
