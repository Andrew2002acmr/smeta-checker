#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${SMETA_REPO_DIR:-/root/smeta-checker-dev}"
BRANCH="${SMETA_DEPLOY_BRANCH:-dev}"
LOG_FILE="${SMETA_DEPLOY_LOG:-/var/log/smeta-deploy.log}"
BACKUP_DIR="${SMETA_BACKUP_DIR:-/root/smeta-deploy-backups}"
LOCK_FILE="${SMETA_DEPLOY_LOCK:-/run/smeta-deploy.lock}"
MAX_BACKUPS="${SMETA_MAX_BACKUPS:-5}"

# Optional compose overlays (monitoring + self-hosted GlitchTip) are enabled by
# default. Override with SMETA_COMPOSE_FILES="" to disable them.
COMPOSE=(docker compose)
COMPOSE+=(-f compose.yaml)
for f in ${SMETA_COMPOSE_FILES:-compose.monitoring.yaml compose.glitchtip.yaml}; do
  if [ -f "$f" ]; then
    COMPOSE+=(-f "$f")
  fi
done

log() { printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$LOG_FILE"; }

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "deploy already running; skipping"
  exit 0
fi

cd "$REPO_DIR"

CURRENT="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH" 2>/dev/null || true)"

if [ -z "$REMOTE" ]; then
  log "cannot resolve origin/$BRANCH; aborting"
  exit 1
fi

if [ "$CURRENT" = "$REMOTE" ]; then
  exit 0
fi

log "deploying $CURRENT -> $REMOTE"

STAMP="$(date -u +'%Y%m%dT%H%M%SZ')"
STAGE="$BACKUP_DIR/$STAMP"
mkdir -p "$STAGE"

"${COMPOSE[@]}" exec -T db pg_dump \
  -U "${POSTGRES_USER:-smeta}" \
  -d "${POSTGRES_DB:-smeta}" \
  --format=custom --compress=6 --file=/tmp/smeta.dump >/dev/null
"${COMPOSE[@]}" cp db:/tmp/smeta.dump "$STAGE/db.dump" >/dev/null
"${COMPOSE[@]}" exec -T db rm -f /tmp/smeta.dump >/dev/null
"${COMPOSE[@]}" cp api:/srv/smeta/.audit_blobs/. "$STAGE/blobs/" >/dev/null 2>&1 || true
printf 'previous_commit=%s\nnew_commit=%s\ncreated_utc=%s\n' "$CURRENT" "$REMOTE" "$STAMP" > "$STAGE/manifest.txt"

for name in api workspace worker-pdf worker-llm migrate; do
  image="$(docker inspect --format '{{.Image}}' "smeta-checker-$name-1" 2>/dev/null || true)"
  if [ -n "$image" ]; then
    docker image tag "$image" "smeta-rollback-$name:$STAMP" >/dev/null 2>&1 || true
  fi
done

git fetch origin
git reset --hard "origin/$BRANCH"

if ! "${COMPOSE[@]}" build 2>&1 | tail -20 >>"$LOG_FILE"; then
  log "build failed; rolling back code"
  git reset --hard "$CURRENT"
  "${COMPOSE[@]}" up -d --no-build 2>&1 | tail -5 >>"$LOG_FILE" || true
  exit 1
fi

if ! "${COMPOSE[@]}" up -d --no-build --wait --wait-timeout 180 2>&1 | tail -20 >>"$LOG_FILE"; then
  log "restart failed; rolling back"
  git reset --hard "$CURRENT"
  "${COMPOSE[@]}" up -d --no-build 2>&1 | tail -5 >>"$LOG_FILE" || true
  exit 1
fi

if ! "${COMPOSE[@]}" exec -T api curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
  log "api health check failed; rolling back"
  git reset --hard "$CURRENT"
  "${COMPOSE[@]}" up -d --no-build 2>&1 | tail -5 >>"$LOG_FILE" || true
  exit 1
fi

if ! "${COMPOSE[@]}" exec -T workspace node -e "fetch('http://127.0.0.1:3000/').then(r=>{if(!r.ok)process.exit(1)})" >/dev/null 2>&1; then
  log "workspace health check failed; rolling back"
  git reset --hard "$CURRENT"
  "${COMPOSE[@]}" up -d --no-build 2>&1 | tail -5 >>"$LOG_FILE" || true
  exit 1
fi

# Auto-provision self-hosted GlitchTip (if enabled) and inject its DSN into .env.
if "${COMPOSE[@]}" ps --services 2>/dev/null | grep -qx glitchtip; then
  for i in $(seq 1 30); do
    if "${COMPOSE[@]}" exec -T glitchtip python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/_health/")' >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done

  dsn="$("${COMPOSE[@]}" exec -T glitchtip python manage.py shell <<'PY' 2>/dev/null | sed -n 's/^DSN=//p' | tail -1
from allauth.account.models import EmailAddress
from apps.organizations_ext.models import Organization
from apps.projects.models import Project, ProjectKey
from apps.teams.models import Team
from apps.users.models import User

email = "admin@localhost"
password = "change-me"
org_slug = "smeta"
project_slug = "smeta"

user, created = User.objects.get_or_create(email=email, defaults={"is_staff": True, "is_superuser": True})
if created:
    user.set_password(password)
    user.save()
EmailAddress.objects.get_or_create(user=user, email=email, defaults={"primary": True, "verified": True})

org, _ = Organization.objects.get_or_create(slug=org_slug, defaults={"name": org_slug})
if not org.users.filter(pk=user.pk).exists():
    org.add_user(user)

project, _ = Project.objects.get_or_create(slug=project_slug, organization=org, defaults={"name": project_slug})

team, _ = Team.objects.get_or_create(slug=project_slug, organization=org, defaults={})
org_user = org.organization_users.get(user=user)
team.members.add(org_user)
team.projects.add(project)

key = ProjectKey.objects.filter(project=project).first()
print("DSN=" + (key.get_dsn() if key else ""))
PY
)"

  if [ -n "$dsn" ]; then
    old_dsn="$(grep -E '^GLITCHTIP_DSN=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)"
    if [ "$old_dsn" != "$dsn" ]; then
      if grep -q '^GLITCHTIP_DSN=' .env 2>/dev/null; then
        sed -i "s|^GLITCHTIP_DSN=.*|GLITCHTIP_DSN=$dsn|" .env
      else
        printf 'GLITCHTIP_DSN=%s\n' "$dsn" >> .env
      fi
      if grep -q '^NUXT_PUBLIC_SENTRY_DSN=' .env 2>/dev/null; then
        sed -i "s|^NUXT_PUBLIC_SENTRY_DSN=.*|NUXT_PUBLIC_SENTRY_DSN=$dsn|" .env
      else
        printf 'NUXT_PUBLIC_SENTRY_DSN=%s\n' "$dsn" >> .env
      fi
      "${COMPOSE[@]}" up -d --no-build api workspace >/dev/null 2>&1 || true
      log "glitchtip DSN provisioned: $dsn"
    fi
  else
    log "glitchtip DSN could not be determined"
  fi
fi

log "deployed successfully: $CURRENT -> $REMOTE"

ls -1dt "$BACKUP_DIR"/*/ 2>/dev/null | tail -n +"$((MAX_BACKUPS + 1))" | xargs -r rm -rf

exit 0