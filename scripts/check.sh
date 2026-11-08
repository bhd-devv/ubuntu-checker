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
export TELEGRAM_POSTGRES_PASSWORD=validation-only
docker compose config --quiet
docker compose -f compose.setup.yml config --quiet
docker compose -f docker-compose.yml -f compose.local.yml config --quiet
docker compose -f compose.setup.yml -f compose.local.yml config --quiet
docker compose -f compose.test.yml config --quiet

# Run the same behavior/transaction tests against SQLite and real PostgreSQL.
# Unique project + scoped cleanup never touch deployment containers or volumes.
CHECK_PROJECT="ubuntu-checker-check-$$"
cleanup() {
  docker compose -p "$CHECK_PROJECT" -f compose.test.yml down --volumes
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
docker compose -p "$CHECK_PROJECT" -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests

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

printf '%s\n' 'Compose, Telegram bot (SQLite + PostgreSQL), Prometheus rules and alert scenarios: OK'
