#!/bin/sh
set -e

python manage.py migrate --noinput
python manage.py collectstatic --noinput
# Снимки прокторинга старше SNAPSHOT_RETENTION_DAYS: при старте и раз в сутки.
# Не отдельный cron-сервис: том со снимками подключён только к этому контейнеру.
(while true; do python manage.py cleanup_snapshots || true; sleep 86400; done) &

# ASGI: HTTP + WebSocket (прокторинг). Для нескольких инстансов нужен REDIS_URL.
exec daphne -b 0.0.0.0 -p 8000 config.asgi:application
