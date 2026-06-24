#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${NIMBLE_REVIEWER_DIR:-/mnt/nimble-reviewer}"
BACKUP_ROOT="${1:-${NIMBLE_REVIEWER_BACKUP_ROOT:-/mnt/nimble-reviewer-runtime-backups}}"
HELPER_IMAGE="${BACKUP_HELPER_IMAGE:-nimble-reviewer:latest}"
CONTAINER="${NIMBLE_REVIEWER_CONTAINER:-nimble-reviewer}"
STOP_CONTAINER="${BACKUP_STOP_CONTAINER:-1}"
VOLUMES=(
  nimble-reviewer-data
  nimble-reviewer-cache
  nimble-reviewer-auth
  nimble-reviewer-claude-auth
)

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="${BACKUP_ROOT%/}/${timestamp}"

mkdir -p "$backup_dir"
chmod 700 "$backup_dir"

{
  echo "created_at_utc=${timestamp}"
  echo "host=$(hostname)"
  echo "app_dir=${APP_DIR}"
  echo "helper_image=${HELPER_IMAGE}"
  echo "volumes=${VOLUMES[*]}"
} >"${backup_dir}/manifest.txt"

container_was_running=0
if [[ "$STOP_CONTAINER" == "1" ]] && docker inspect "$CONTAINER" >/dev/null 2>&1; then
  if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" == "true" ]]; then
    docker stop "$CONTAINER" >/dev/null
    container_was_running=1
    trap 'docker start "$CONTAINER" >/dev/null || true' EXIT
  fi
fi

if [[ -f "${APP_DIR}/.env" ]]; then
  cp -p "${APP_DIR}/.env" "${backup_dir}/.env"
  chmod 600 "${backup_dir}/.env"
else
  echo "warning: ${APP_DIR}/.env does not exist; backup will not include env file" >&2
fi

for volume in "${VOLUMES[@]}"; do
  docker volume inspect "$volume" >/dev/null
  docker run --rm --user 0 \
    -v "${volume}:/volume:ro" \
    -v "${backup_dir}:/backup" \
    "$HELPER_IMAGE" \
    tar -C /volume -czf "/backup/${volume}.tgz" .
  chmod 600 "${backup_dir}/${volume}.tgz"
done

if [[ "$container_was_running" == "1" ]]; then
  docker start "$CONTAINER" >/dev/null
  trap - EXIT
fi

echo "$backup_dir"
