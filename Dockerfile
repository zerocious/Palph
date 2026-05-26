# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

# Standard Python container hygiene
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install deps first — separate layer so dep changes don't bust the code layer
COPY requirements.txt .
RUN pip install -r requirements.txt

# Non-root user (security best practice). Owns /app (code) and /app/data (state).
RUN useradd --create-home --shell /bin/bash app \
    && mkdir -p /app/data \
    && chown -R app:app /app/data

# Project code
COPY --chown=app:app . .

USER app

# Paths to persistent state — bothost.ru mounts /app/data; docker-compose
# binds ./data:/app/data for local Docker dev.
ENV DB_PATH=/app/data/studybuddy.db \
    LOG_FILE=/app/data/bot.log \
    BACKUP_DIR=/app/data/backups

CMD ["python", "bot.py"]
