#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
API_DIR="$ROOT_DIR/api"
WEB_DIR="$ROOT_DIR/web"
API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3003}"
API_PID=""
WEB_PID=""

cleanup() {
  trap - INT TERM EXIT
  for pid in "$WEB_PID" "$API_PID"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
}

fail() {
  printf 'SQL Assistant could not start: %s\n' "$1" >&2
  exit 1
}

[[ -x "$API_DIR/.venv/bin/uvicorn" ]] || fail "Python API environment is missing. See README.md setup instructions."
[[ -f "$API_DIR/.env" ]] || fail "api/.env is missing. Copy api/.env.example to api/.env and add your OpenAI key."
[[ -d "$WEB_DIR/node_modules" ]] || fail "web/node_modules is missing. Run npm install in web once."

# Create SQL Assistant's private app-store credentials once. The connected target DB is separate.
if ! grep -Eq '^SQL_ASSISTANT_POSTGRES_PASSWORD=.+$' "$API_DIR/.env"; then
  printf '\nSQL_ASSISTANT_POSTGRES_PASSWORD=%s\n' "$(python3 -c 'import secrets; print(secrets.token_hex(24))')" >> "$API_DIR/.env"
fi
if ! grep -Eq '^APP_DATABASE_URL=.+$' "$API_DIR/.env"; then
  pg_password="$(sed -n 's/^SQL_ASSISTANT_POSTGRES_PASSWORD=//p' "$API_DIR/.env" | tail -n 1)"
  printf 'APP_DATABASE_URL=postgresql://sql_assistant:%s@127.0.0.1:5434/sql_assistant\n' "$pg_password" >> "$API_DIR/.env"
fi
chmod 600 "$API_DIR/.env"

app_store_url="$(sed -n 's/^APP_DATABASE_URL=//p' "$API_DIR/.env" | tail -n 1)"
if [[ "$app_store_url" == postgresql://sql_assistant:*@127.0.0.1:5434/sql_assistant ]]; then
  command -v docker >/dev/null 2>&1 || fail "Docker is required for the SQL Assistant workspace/chat store. Start Docker Desktop and retry."
  docker info >/dev/null 2>&1 || fail "Docker Desktop is not running. Start it and retry."
  printf 'Starting SQL Assistant PostgreSQL storage in Docker.\n'
  docker compose --env-file "$API_DIR/.env" -f "$ROOT_DIR/docker-compose.yml" up -d postgres
  postgres_ready=0
  for _ in {1..60}; do
    if docker compose --env-file "$API_DIR/.env" -f "$ROOT_DIR/docker-compose.yml" exec -T postgres pg_isready -U sql_assistant -d sql_assistant >/dev/null 2>&1; then
      postgres_ready=1
      break
    fi
    sleep 1
  done
  [[ "$postgres_ready" -eq 1 ]] || fail "SQL Assistant PostgreSQL did not become ready within 60 seconds."
fi

command -v lsof >/dev/null 2>&1 || fail "lsof is required to safely restart the app and verify port ownership."

stop_project_service() {
  local port="$1"
  local label="$2"
  local expected_dir="$3"
  local pids pid cwd

  pids="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)"
  [[ -n "$pids" ]] || return 0

  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
    case "$cwd" in
      "$expected_dir"|"$expected_dir"/*)
        printf 'Restarting existing %s process (PID %s).\n' "$label" "$pid"
        kill -TERM "$pid" 2>/dev/null || true
        ;;
      *)
        fail "port $port is in use by a process outside this SQL Assistant project; it was left untouched."
        ;;
    esac
  done <<< "$pids"

  for _ in {1..50}; do
    if ! lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  fail "the previous $label process did not release port $port."
}

stop_project_service "$API_PORT" "API" "$API_DIR"
stop_project_service "$WEB_PORT" "web app" "$WEB_DIR"

trap cleanup INT TERM EXIT
printf 'Starting SQL Assistant API on http://127.0.0.1:%s\n' "$API_PORT"
(cd "$API_DIR" && exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" --reload) &
API_PID=$!

printf 'Starting SQL Assistant web app on http://localhost:%s\n' "$WEB_PORT"
(cd "$WEB_DIR" && exec npm run dev -- --hostname 127.0.0.1 --port "$WEB_PORT") &
WEB_PID=$!

printf '\nBoth services are running. Press Ctrl+C to stop them.\n\n'
wait "$API_PID" "$WEB_PID"
