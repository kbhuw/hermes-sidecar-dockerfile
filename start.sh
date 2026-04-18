#!/bin/bash
# Boot Hermes' built-in api_server on 127.0.0.1:8642 in the background,
# then run the dashboard sidecar (file CRUD + reverse proxy) on 0.0.0.0:8080.
set -e

HERMES_HOME="${HERMES_HOME:-/opt/data}"
INSTALL_DIR="/opt/hermes"
API_SERVER_PORT="${API_SERVER_PORT:-8642}"
DASHBOARD_PORT="${HERMES_DASHBOARD_PORT:-8080}"

# Drop to hermes user via the upstream entrypoint when we're running as root.
if [ "$(id -u)" = "0" ]; then
    if [ -n "$HERMES_UID" ] && [ "$HERMES_UID" != "$(id -u hermes)" ]; then
        usermod -u "$HERMES_UID" hermes
    fi
    if [ -n "$HERMES_GID" ] && [ "$HERMES_GID" != "$(id -g hermes)" ]; then
        groupmod -o -g "$HERMES_GID" hermes 2>/dev/null || true
    fi
    actual_hermes_uid=$(id -u hermes)
    if [ "$(stat -c %u "$HERMES_HOME" 2>/dev/null)" != "$actual_hermes_uid" ]; then
        chown -R hermes:hermes "$HERMES_HOME" 2>/dev/null || true
    fi
    exec gosu hermes "$0" "$@"
fi

source "${INSTALL_DIR}/.venv/bin/activate"

mkdir -p "$HERMES_HOME"/{cron,sessions,logs,hooks,memories,skills,skins,plans,workspace,home}

[ -f "$HERMES_HOME/.env" ] || cp "$INSTALL_DIR/.env.example" "$HERMES_HOME/.env"
[ -f "$HERMES_HOME/config.yaml" ] || cp "$INSTALL_DIR/cli-config.yaml.example" "$HERMES_HOME/config.yaml"
[ -f "$HERMES_HOME/SOUL.md" ] || cp "$INSTALL_DIR/docker/SOUL.md" "$HERMES_HOME/SOUL.md"
[ -d "$INSTALL_DIR/skills" ] && python3 "$INSTALL_DIR/tools/skills_sync.py" || true

if [ -z "$API_SERVER_KEY" ]; then
    echo "FATAL: API_SERVER_KEY must be set"
    exit 1
fi

echo "[start.sh] Starting Hermes gateway on 127.0.0.1:${API_SERVER_PORT} (api_server enabled)"
hermes gateway &
HERMES_PID=$!

trap 'kill $HERMES_PID 2>/dev/null || true' EXIT

# Wait for api_server to be reachable before exposing the sidecar.
for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${API_SERVER_PORT}/health" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

echo "[start.sh] Starting sidecar on 0.0.0.0:${DASHBOARD_PORT}"
exec python3 /opt/hermes-sidecar/sidecar.py
