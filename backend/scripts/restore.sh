#!/usr/bin/env bash
# issue 22: restore Postgres + blobs from ./backend/scripts/backup.sh output.
# Usage: ./backend/scripts/restore.sh <stamp-dir>
# Stops api/workers during restore, keeps db/redis running.
set -euo pipefail

cd "$(dirname "$0")/../.."
SRC="${1:?Usage: $0 <backup-dir> (from ./backend/scripts/backup.sh)}"
test -f "$SRC/db.dump" || { echo "no db.dump in $SRC" >&2; exit 1; }
test -f "$SRC/manifest.txt" || { echo "no manifest.txt in $SRC" >&2; exit 1; }

echo "==> manifest:"
cat "$SRC/manifest.txt"

echo "==> stopping api + workspace + workers"
docker compose stop api workspace worker-pdf worker-llm

echo "==> restoring db (drop + pg_restore)"
docker compose cp "$SRC/db.dump" db:/tmp/restore.dump
docker compose exec -T db sh -c \
  'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB" && pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner /tmp/restore.dump && rm -f /tmp/restore.dump'

echo "==> restoring blobs"
docker compose start api
# compose cp lands files as root; fix ownership as root (image user is app).
docker compose exec -T -u root api sh -c 'rm -rf /srv/smeta/.audit_blobs/* && mkdir -p /srv/smeta/.audit_blobs'
docker compose cp "$SRC/blobs/." api:/srv/smeta/.audit_blobs/
docker compose exec -T -u root api chown -R app:app /srv/smeta/.audit_blobs

echo "==> restarting stack"
docker compose up -d api workspace worker-pdf worker-llm
docker compose ps
echo "==> restore done from $SRC"
