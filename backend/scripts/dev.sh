#!/usr/bin/env bash
# Dev entrypoint: bring the stack up, auto-restore the newest backup into
# a fresh database. No-op when data already exists or no backup is present.
#
# Usage:
#   ./backend/scripts/dev.sh            # newest ./backups/*/ if db is empty
#   ./backend/scripts/dev.sh <stamp>    # force this backup (wipe + restore)
#   ./backend/scripts/dev.sh --fresh    # wipe volumes, start empty
set -euo pipefail
cd "$(dirname "$0")/../.."

FORCE=""
FRESH=0
for arg in "$@"; do
  case "$arg" in
    --fresh) FRESH=1 ;;
    --help|-h) sed -n '2,8p' "$0"; exit 0 ;;
    *) FORCE="$arg" ;;
  esac
done

if [ "$FRESH" = 1 ]; then
  echo "==> fresh: wiping all data"
  docker compose down -q
  docker volume rm -f smeta-checker_pgdata smeta-checker_blobs \
    smeta-checker_pdfcache smeta-checker_redisdata >/dev/null 2>&1 || true
  docker compose up -d --build
  echo "==> stack is up empty: http://127.0.0.1:3000"
  exit 0
fi

newest_backup() {
  for d in $(ls -dt backups/*/ 2>/dev/null); do
    if [ -f "$d/db.dump" ] && [ -f "$d/manifest.txt" ]; then
      echo "${d%/}"
      return 0
    fi
  done
  return 1
}

db_is_empty() {
  # No pgdata volume, or volume without migrated tables -> fresh.
  docker volume inspect smeta-checker_pgdata >/dev/null 2>&1 || return 0
  docker compose up -d db >/dev/null 2>&1
  for _ in $(seq 1 30); do
    docker compose exec -T db pg_isready -U "${POSTGRES_USER:-smeta}" >/dev/null 2>&1 && break
    sleep 2
  done
  local tables
  tables=$(docker compose exec -T db psql -U "${POSTGRES_USER:-smeta}" \
    -d "${POSTGRES_DB:-smeta}" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name <> 'alembic_version'" 2>/dev/null || echo 0)
  [ "${tables// /}" = "0" ]
}

if [ -n "$FORCE" ]; then
  echo "==> forced restore from $FORCE"
  docker compose up -d db >/dev/null 2>&1
  ./backend/scripts/restore.sh "$FORCE"
  exit 0
fi

if db_is_empty; then
  if SRC=$(newest_backup); then
    echo "==> fresh database, auto-restoring $SRC"
    ./backend/scripts/restore.sh "$SRC"
    exit 0
  fi
  echo "==> fresh database, no backup found — starting empty"
else
  echo "==> database has data — starting normally"
fi
docker compose up -d --build
echo "==> stack is up: http://127.0.0.1:3000"
