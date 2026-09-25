FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DOWNLOAD_DIR=/data/downloads
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements-bot.txt .
RUN pip install --no-cache-dir -r requirements-bot.txt
COPY app ./app
COPY qq_bot ./qq_bot
COPY static ./static
COPY assets ./assets
RUN mkdir -p /data/downloads
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
