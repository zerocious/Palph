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

# Non-root user (security best practice). Owns both /app (code) and /data (state).
RUN useradd --create-home --shell /bin/bash app \
    && mkdir -p /data \
    && chown -R app:app /data

# Project code
COPY --chown=app:app . .

USER app

# Paths to persistent state — overridden in docker-compose so SQLite DB and
# logs live on a mounted volume, not in the ephemeral container layer.
ENV DB_PATH=/data/studybuddy.db \
    LOG_FILE=/data/bot.log

CMD ["python", "bot.py"]
