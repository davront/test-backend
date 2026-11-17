#!/bin/sh
set -e

python manage.py migrate --noinput
python manage.py collectstatic --noinput
# Снимки прокторинга старше SNAPSHOT_RETENTION_DAYS; при деплое + нужен ежедневный cron
python manage.py cleanup_snapshots || true

# ASGI: HTTP + WebSocket (прокторинг). Для нескольких инстансов нужен REDIS_URL.
exec daphne -b 0.0.0.0 -p 8000 config.asgi:application
