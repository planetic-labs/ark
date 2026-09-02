#!/usr/bin/env bash
# Create a Restic backup of Ark PostgreSQL data and uploaded files.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORK_DIR="$PROJECT_ROOT/tmp/restic-backup"
DUMP_PATH="$WORK_DIR/ark.postgres.dump"
UPLOADS_PATH="$WORK_DIR/uploads"
LOG_DIR="$PROJECT_ROOT/logs"

if [[ -f "$PROJECT_ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/.env"
    set +a
fi

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/restic_backup.log") 2>&1

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

cleanup() {
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

echo "[$(date --iso-8601=seconds)] Starting Ark Restic backup"
if ! restic snapshots >/dev/null 2>&1; then
    echo "Restic repository is not initialized; initializing it now."
    restic init
fi

POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_DB="${POSTGRES_DB:-ark}"
"${compose[@]}" exec -T db pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null

mkdir -p "$WORK_DIR"
echo "Creating consistent PostgreSQL dump..."
"${compose[@]}" exec -T db pg_dump \
    -U "$POSTGRES_USER" \
    -d "$POSTGRES_DB" \
    --format=custom \
    --no-owner \
    --no-privileges > "$DUMP_PATH"
test -s "$DUMP_PATH"

echo "Copying uploaded files from the API volume..."
API_CONTAINER_ID="$("${compose[@]}" ps -q api)"
if [[ -z "$API_CONTAINER_ID" ]]; then
    echo "ERROR: API container is not running." >&2
    exit 1
fi
mkdir -p "$UPLOADS_PATH"
docker cp "$API_CONTAINER_ID:/app/static/uploads/." "$UPLOADS_PATH/"

targets=("$DUMP_PATH" "$UPLOADS_PATH")
if [[ -f "$PROJECT_ROOT/.env" ]]; then
    targets+=("$PROJECT_ROOT/.env")
fi

echo "Uploading backup to Restic repository..."
restic backup --host "ark-api-dev" --tag "ark-api" --tag "database" "${targets[@]}"

echo "Applying retention policy..."
restic forget \
    --keep-daily "${RESTIC_KEEP_DAILY:-7}" \
    --keep-weekly "${RESTIC_KEEP_WEEKLY:-4}" \
    --keep-monthly "${RESTIC_KEEP_MONTHLY:-12}" \
    --prune

echo "Checking repository integrity..."
restic check --read-data-subset=10%
echo "[$(date --iso-8601=seconds)] Backup finished successfully"
