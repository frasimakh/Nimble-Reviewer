#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${NIMBLE_REVIEWER_DIR:-/mnt/nimble-reviewer}"
IMAGE="${NIMBLE_REVIEWER_IMAGE:-nimble-reviewer:latest}"
CONTAINER="${NIMBLE_REVIEWER_CONTAINER:-nimble-reviewer}"
HOST_PORT="${HOST_PORT:-18080}"
CONTAINER_PORT="${PORT:-8080}"

if [[ ! -f "${APP_DIR}/.env" ]]; then
  echo "missing ${APP_DIR}/.env; restore it or run scripts/restore_runtime_state.sh first" >&2
  exit 1
fi

cd "$APP_DIR"
docker build -t "$IMAGE" .
docker rm -f "$CONTAINER" 2>/dev/null || true
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  -p "${HOST_PORT}:${CONTAINER_PORT}" \
  --env-file "${APP_DIR}/.env" \
  -v nimble-reviewer-data:/data \
  -v nimble-reviewer-cache:/cache \
  -v nimble-reviewer-auth:/home/reviewer/.codex \
  -v nimble-reviewer-claude-auth:/home/reviewer/.claude \
  "$IMAGE"

if [[ "${FOLLOW_LOGS:-0}" == "1" ]]; then
  docker logs -f "$CONTAINER"
fi
