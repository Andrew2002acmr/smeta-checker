#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${SMETA_REPO_DIR:-/root/smeta-checker-dev}"
BRANCH="${SMETA_DEPLOY_BRANCH:-dev}"
LOG_FILE="${SMETA_DEPLOY_LOG:-/var/log/smeta-deploy.log}"
BACKUP_DIR="${SMETA_BACKUP_DIR:-/root/smeta-deploy-backups}"
LOCK_FILE="${SMETA_DEPLOY_LOCK:-/run/smeta-deploy.lock}"
MAX_BACKUPS="${SMETA_MAX_BACKUPS:-5}"

log() { printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$LOG_FILE"; }

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "deploy already running; skipping"
  exit 0
fi

cd "$REPO_DIR"
git fetch origin

CURRENT="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH" 2>/dev/null || true)"

if [ -z "$REMOTE" ]; then
  log "cannot resolve origin/$BRANCH; aborting"
  exit 1
fi


log "deploying $CURRENT -> $REMOTE"

STAMP="$(date -u +'%Y%m%dT%H%M%SZ')"
STAGE="$BACKUP_DIR/$STAMP"
mkdir -p "$STAGE"

docker compose exec -T db pg_dump \
  -U "${POSTGRES_USER:-smeta}" \
  -d "${POSTGRES_DB:-smeta}" \
  --format=custom --compress=6 --file=/tmp/smeta.dump >/dev/null
docker compose cp db:/tmp/smeta.dump "$STAGE/db.dump" >/dev/null
docker compose exec -T db rm -f /tmp/smeta.dump >/dev/null
docker compose cp api:/srv/smeta/.audit_blobs/. "$STAGE/blobs/" >/dev/null 2>&1 || true
printf 'previous_commit=%s\nnew_commit=%s\ncreated_utc=%s\n' "$CURRENT" "$REMOTE" "$STAMP" > "$STAGE/manifest.txt"

for name in api workspace worker-pdf worker-llm migrate; do
  image="$(docker inspect --format '{{.Image}}' "smeta-checker-$name-1" 2>/dev/null || true)"
  if [ -n "$image" ]; then
    docker image tag "$image" "smeta-rollback-$name:$STAMP" >/dev/null 2>&1 || true
  fi
done

git fetch origin
git reset --hard "origin/$BRANCH"

# Build only the services that need building (backend + frontend).
# Monitoring/glitchtip use pre-built images and don't need a build.
if ! timeout 1800 docker compose -f compose.yaml build 2>&1 | tail -20 >>"$LOG_FILE"; then
  rc=${PIPESTATUS[0]}
  log "build failed (rc=$rc); rolling back code"
  git reset --hard "$CURRENT"
  docker compose -f compose.yaml up -d --no-build 2>&1 | tail -5 >>"$LOG_FILE" || true
  exit 1
fi

# Start the main stack first (with healthcheck wait).
if ! docker compose -f compose.yaml up -d --no-build --wait --wait-timeout 180 2>&1 | tail -20 >>"$LOG_FILE"; then
  log "restart failed; rolling back"
  git reset --hard "$CURRENT"
  docker compose -f compose.yaml up -d --no-build 2>&1 | tail -5 >>"$LOG_FILE" || true
  exit 1
fi

# Start monitoring + glitchtip (no build, no wait — they use pre-built images).
docker compose -f compose.yaml -f compose.monitoring.yaml -f compose.glitchtip.yaml up -d --no-build 2>&1 | tail -20 >>"$LOG_FILE" || true

if ! docker compose exec -T api curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
  log "api health check failed; rolling back"
  git reset --hard "$CURRENT"
  docker compose -f compose.yaml -f compose.monitoring.yaml -f compose.glitchtip.yaml up -d --no-build 2>&1 | tail -5 >>"$LOG_FILE" || true
  exit 1
fi

if ! docker compose exec -T workspace node -e "fetch('http://127.0.0.1:3000/').then(r=>{if(!r.ok)process.exit(1)})" >/dev/null 2>&1; then
  log "workspace health check failed; rolling back"
  git reset --hard "$CURRENT"
  docker compose -f compose.yaml -f compose.monitoring.yaml -f compose.glitchtip.yaml up -d --no-build 2>&1 | tail -5 >>"$LOG_FILE" || true
  exit 1
fi

log "deployed successfully: $CURRENT -> $REMOTE"

ls -1dt "$BACKUP_DIR"/*/ 2>/dev/null | tail -n +"$((MAX_BACKUPS + 1))" | xargs -r rm -rf

exit 0
