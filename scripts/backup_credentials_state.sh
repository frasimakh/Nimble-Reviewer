#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${NIMBLE_REVIEWER_DIR:-/mnt/nimble-reviewer}"
BACKUP_ROOT="${1:-${NIMBLE_REVIEWER_CREDENTIALS_BACKUP_ROOT:-/mnt/nimble-reviewer-credentials-backups}}"
HELPER_IMAGE="${BACKUP_HELPER_IMAGE:-nimble-reviewer:latest}"
VOLUMES=(
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

echo "$backup_dir"
