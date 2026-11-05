#!/usr/bin/env bash
# Provision GlitchTip on the observability VPS: wait for health, create
# admin user + smeta org/project via Django shell, print the DSN and lock
# registration down. Run on the server from the observability/ directory.
set -euo pipefail

ADMIN_EMAIL="${GLITCHTIP_ADMIN_EMAIL:-admin@localhost}"
ADMIN_PASSWORD="${GLITCHTIP_ADMIN_PASSWORD:-change-me}"
ORG_SLUG="${GLITCHTIP_ORG_SLUG:-smeta}"
PROJECT_SLUG="${GLITCHTIP_PROJECT_SLUG:-smeta}"

echo "waiting for glitchtip health..."
for i in $(seq 1 30); do
  if docker compose exec -T glitchtip python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/_health/")' >/dev/null 2>&1; then
    break
  fi
  sleep 2
  if [ "$i" = 30 ]; then echo "glitchtip did not become healthy" >&2; exit 1; fi
done

dsn="$(docker compose exec -T glitchtip python manage.py shell <<PY 2>/dev/null | sed -n 's/^DSN=//p' | tail -1
from allauth.account.models import EmailAddress
from apps.organizations_ext.models import Organization
from apps.projects.models import Project, ProjectKey
from apps.teams.models import Team
from apps.users.models import User

email = "$ADMIN_EMAIL"
password = """$ADMIN_PASSWORD"""

user, created = User.objects.get_or_create(email=email, defaults={"is_staff": True, "is_superuser": True})
if created:
    user.set_password(password)
    user.save()
EmailAddress.objects.get_or_create(user=user, email=email, defaults={"primary": True, "verified": True})

org, _ = Organization.objects.get_or_create(slug="$ORG_SLUG", defaults={"name": "$ORG_SLUG"})
if not org.users.filter(pk=user.pk).exists():
    org.add_user(user)

project, _ = Project.objects.get_or_create(slug="$PROJECT_SLUG", organization=org, defaults={"name": "$PROJECT_SLUG"})

team, _ = Team.objects.get_or_create(slug="$PROJECT_SLUG", organization=org, defaults={})
org_user = org.organization_users.get(user=user)
team.members.add(org_user)
team.projects.add(project)

key = ProjectKey.objects.filter(project=project).first()
print("DSN=" + (key.get_dsn() if key else ""))
PY
)"

if [ -z "$dsn" ]; then echo "DSN could not be determined" >&2; exit 1; fi
echo "GLITCHTIP_DSN=$dsn"
echo "Set GLITCHTIP_ENABLE_USER_REGISTRATION=false in .env and restart glitchtip."
