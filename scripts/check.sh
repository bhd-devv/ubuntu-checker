#!/bin/sh
set -eu

TASK_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$TASK_ROOT"

# Placeholders are used only to validate Compose, never to start services.
export BESZEL_APP_URL=https://monitor.example.com
export BESZEL_USER_EMAIL=validation@example.com
export BESZEL_USER_PASSWORD=validation-only
export BESZEL_AGENT_KEY=validation-only
export TELEGRAM_BOT_TOKEN=validation-only
export TELEGRAM_CHAT_ID=validation-only
docker compose config --quiet
docker compose -f compose.setup.yml config --quiet
docker compose -f docker-compose.yml -f compose.local.yml config --quiet
docker compose -f compose.setup.yml -f compose.local.yml config --quiet

if command -v python3 >/dev/null 2>&1; then
  PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
else
  docker run --rm --entrypoint python -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$TASK_ROOT:/work:ro" -w /work \
    python:3.14-slim-bookworm -m unittest discover -s tests -p 'test_*.py'
fi

if command -v promtool >/dev/null 2>&1; then
  promtool check config --syntax-only prometheus/prometheus.yml
  promtool check rules prometheus/alerts.yml
  promtool test rules tests/alerts.test.yml
else
  docker run --rm --entrypoint /bin/promtool \
    -v "$TASK_ROOT/prometheus:/etc/prometheus:ro" \
    prom/prometheus:v3.15.0 check config /etc/prometheus/prometheus.yml
  docker run --rm --entrypoint /bin/promtool \
    -v "$TASK_ROOT:/work:ro" -w /work \
    prom/prometheus:v3.15.0 test rules tests/alerts.test.yml
fi

printf '%s\n' 'Compose, Telegram bot, Prometheus rules and alert scenarios: OK'
