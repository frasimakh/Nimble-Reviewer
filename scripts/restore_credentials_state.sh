#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 /path/to/credentials-backup-dir" >&2
  exit 2
fi

BACKUP_DIR="${1%/}"
APP_DIR="${NIMBLE_REVIEWER_DIR:-/mnt/nimble-reviewer}"
HELPER_IMAGE="${BACKUP_HELPER_IMAGE:-nimble-reviewer:latest}"
VOLUMES=(
  nimble-reviewer-auth
  nimble-reviewer-claude-auth
)

if [[ ! -d "$BACKUP_DIR" ]]; then
  echo "backup directory does not exist: $BACKUP_DIR" >&2
  exit 1
fi

mkdir -p "$APP_DIR"

if [[ -f "${BACKUP_DIR}/.env" ]]; then
  if [[ -f "${APP_DIR}/.env" && "${RESTORE_OVERWRITE_ENV:-0}" != "1" ]]; then
    echo "refusing to overwrite ${APP_DIR}/.env; set RESTORE_OVERWRITE_ENV=1 to replace it" >&2
    exit 1
  fi
  cp -p "${BACKUP_DIR}/.env" "${APP_DIR}/.env"
  chmod 600 "${APP_DIR}/.env"
fi

for volume in "${VOLUMES[@]}"; do
  archive="${BACKUP_DIR}/${volume}.tgz"
  if [[ ! -f "$archive" ]]; then
    echo "warning: missing archive ${archive}; skipping ${volume}" >&2
    continue
  fi

  docker volume create "$volume" >/dev/null
  docker run --rm --user 0 \
    -v "${volume}:/volume" \
    -v "${BACKUP_DIR}:/backup:ro" \
    "$HELPER_IMAGE" \
    tar -C /volume -xzf "/backup/${volume}.tgz"
done

echo "restored credentials state from ${BACKUP_DIR}"
