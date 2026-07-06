FROM python:3.12-slim
RUN apt-get update -qq && \
    apt-get install -y -qq ffmpeg fonts-dejavu-core && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
    httpx Pillow
