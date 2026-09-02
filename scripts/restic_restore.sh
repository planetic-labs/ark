#!/usr/bin/env bash
# Restore an Ark PostgreSQL dump and uploaded files from a Restic snapshot.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SNAPSHOT="latest"
APPLY=0

for argument in "$@"; do
    case "$argument" in
        --apply) APPLY=1 ;;
        -h|--help)
            echo "Usage: $0 [snapshot-id] [--apply]"
            exit 0
            ;;
        *) SNAPSHOT="$argument" ;;
    esac
done

if [[ -f "$PROJECT_ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/.env"
    set +a
fi

compose=()
if docker compose version >/dev/null 2>&1; then
    compose=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    compose=(docker-compose)
else
    echo "ERROR: Docker Compose is required." >&2
    exit 1
fi

if ! command -v restic >/dev/null 2>&1; then
    echo "ERROR: restic is not installed." >&2
    exit 1
fi

if [[ -z "${RESTIC_REPOSITORY:-}" || -z "${RESTIC_PASSWORD:-}" ]]; then
    echo "ERROR: RESTIC_REPOSITORY and RESTIC_PASSWORD must be set in .env." >&2
    exit 1
fi

if [[ -z "${S3_ACCESS_KEY:-}" || -z "${S3_SECRET_KEY:-}" ]]; then
    echo "ERROR: S3_ACCESS_KEY and S3_SECRET_KEY must be set in .env." >&2
    exit 1
fi

export AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY"
export AWS_SECRET_ACCESS_KEY="$S3_SECRET_KEY"
if [[ -n "${S3_REGION_NAME:-}" ]]; then
    export AWS_DEFAULT_REGION="$S3_REGION_NAME"
fi

RESTORE_DIR="$(mktemp -d "$PROJECT_ROOT/tmp/restic-restore.XXXXXX")"
DUMP_PATH="$RESTORE_DIR$PROJECT_ROOT/tmp/restic-backup/ark.postgres.dump"
UPLOADS_PATH="$RESTORE_DIR$PROJECT_ROOT/tmp/restic-backup/uploads"
SERVICES_STOPPED=0

restart_services() {
    if [[ "$SERVICES_STOPPED" -eq 1 ]]; then
        "${compose[@]}" up -d api worker >/dev/null || true
    fi
}
trap restart_services EXIT

echo "Available Ark snapshots:"
restic snapshots --tag ark-api
echo "Restoring snapshot '$SNAPSHOT' into $RESTORE_DIR..."
restic restore "$SNAPSHOT" --target "$RESTORE_DIR"

if [[ ! -s "$DUMP_PATH" || ! -d "$UPLOADS_PATH" ]]; then
    echo "ERROR: Snapshot is incomplete; PostgreSQL dump and uploads are required." >&2
    exit 1
fi
pg_restore --list "$DUMP_PATH" >/dev/null
echo "Snapshot preflight passed. No live data has been changed."

if [[ "$APPLY" -ne 1 ]]; then
    echo "Staging restore is ready at: $RESTORE_DIR"
    echo "Re-run with --apply to restore this snapshot into the active database."
    exit 0
fi

read -r -p "Type RESTORE to replace the active PostgreSQL data and uploads: " CONFIRM
if [[ "$CONFIRM" != "RESTORE" ]]; then
    echo "Restore cancelled. Staging data remains at: $RESTORE_DIR"
    exit 0
fi

POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_DB="${POSTGRES_DB:-ark}"
echo "Stopping API and worker..."
"${compose[@]}" stop api worker >/dev/null
SERVICES_STOPPED=1

echo "Restoring PostgreSQL database..."
"${compose[@]}" exec -T db pg_restore \
        --clean --if-exists --exit-on-error -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        < "$DUMP_PATH"

echo "Restoring uploads volume..."
"${compose[@]}" run --rm --no-deps \
    -v "$UPLOADS_PATH:/restore:ro" \
    --entrypoint sh api \
    -c 'find /app/static/uploads -mindepth 1 -delete && cp -a /restore/. /app/static/uploads/'

echo "Starting API and worker..."
"${compose[@]}" up -d api worker
SERVICES_STOPPED=0
echo "Restore completed successfully. Staging data remains at: $RESTORE_DIR"
