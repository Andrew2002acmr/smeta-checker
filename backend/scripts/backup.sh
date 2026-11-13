#!/usr/bin/env bash
# issue 22: backup Postgres + blobs into ./backups/<stamp>/.
# Usage: ./backend/scripts/backup.sh [output-dir]
# Restores with ./backend/scripts/restore.sh <stamp-dir>. Stops nothing: pg_dump runs
# against the live db container, blobs copy with rsync semantics via cp.
set -euo pipefail

cd "$(dirname "$0")/../.."
OUT="${1:-./backups}/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT/blobs"

echo "==> pg_dump -> $OUT/db.dump"
docker compose exec -T db pg_dump \
  -U "${POSTGRES_USER:-smeta}" \
  -d "${POSTGRES_DB:-smeta}" \
  --format=custom --compress=6 --file=/tmp/smeta.dump
docker compose cp db:/tmp/smeta.dump "$OUT/db.dump"
docker compose exec -T db rm -f /tmp/smeta.dump

echo "==> blobs -> $OUT/blobs/"
docker compose cp api:/srv/smeta/.audit_blobs/. "$OUT/blobs/"

cat > "$OUT/manifest.txt" <<EOF
created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
postgres_db=${POSTGRES_DB:-smeta}
compose_project=${COMPOSE_PROJECT_NAME:-smeta-checker}
EOF

echo "==> backup done: $OUT"
ls -la "$OUT"
