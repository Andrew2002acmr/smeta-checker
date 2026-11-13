#!/usr/bin/env bash
# issue 22: live verification of the compose stack. Run with the stack up:
#   docker compose up -d --build && ./backend/scripts/verify.sh
# Uses only local fixtures + test token from .env; no LLM quota spent.
set -euo pipefail
cd "$(dirname "$0")/../.."

TOKEN="${AUDIT_API_TOKEN:?AUDIT_API_TOKEN must be set (from .env)}"
BASE="http://localhost:${API_PORT:-8000}"
AUTH=(-H "Authorization: Bearer $TOKEN")

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1" >&2; exit 1; }

echo "==> 1. healthz open"
test "$(curl -fsS "$BASE/healthz")" = '{"status":"ok"}' && pass "healthz" || fail "healthz"

echo "==> 2. /api/v1 without token -> 401"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/v1/audits/00000000-0000-0000-0000-000000000000")
test "$CODE" = "401" && pass "401 without token" || fail "expected 401, got $CODE"

echo "==> 3. upload VOR + PDF fixtures"
VOR_JSON=$(curl -fsS "${AUTH[@]}" -F project=VERIFY -F kind=vor -F file=@backend/tests/fixtures/verify-vor.csv "$BASE/api/v1/documents")
VOR_ID=$(echo "$VOR_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['version_id'])")
PDF_JSON=$(curl -fsS "${AUTH[@]}" -F project=VERIFY -F kind=pdf -F file=@backend/tests/fixtures/verify-doc.pdf "$BASE/api/v1/documents")
PDF_ID=$(echo "$PDF_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['version_id'])")
test -n "$VOR_ID" && test -n "$PDF_ID" && pass "upload vor=$VOR_ID pdf=$PDF_ID" || fail "upload"

echo "==> 4. preflight binds fixture"
PRE=$(curl -fsS "${AUTH[@]}" -H 'Content-Type: application/json' -d "{\"project\":\"VERIFY\",\"vor_version_id\":\"$VOR_ID\",\"pdf_version_ids\":[\"$PDF_ID\"]}" "$BASE/api/v1/documents/preflight")
echo "$PRE" | python3 -c "import json,sys; assert json.load(sys.stdin)['has_critical_error'] is False" && pass "preflight ok" || fail "preflight: $PRE"

echo "==> 5. audit without LLM key -> 503 llm_not_configured (graceful degrade)"
CODE=$(curl -s -o /tmp/verify-audit.json -w "%{http_code}" "${AUTH[@]}" -H 'Content-Type: application/json' -d "{\"project\":\"VERIFY\",\"vor_version_id\":\"$VOR_ID\",\"pdf_version_ids\":[\"$PDF_ID\"]}" "$BASE/api/v1/audits")
grep -q llm_not_configured /tmp/verify-audit.json && test "$CODE" = "503" && pass "503 llm_not_configured" || fail "code=$CODE body=$(cat /tmp/verify-audit.json)"

echo "==> 6. restart api -> data survives (upload again reuses version)"
docker compose restart api >/dev/null
for _ in $(seq 1 30); do curl -fsS "$BASE/healthz" >/dev/null 2>&1 && break; sleep 2; done
echo "==> 7. backup produces db.dump + blobs + manifest"
./backend/scripts/backup.sh ./backups >/tmp/verify-backup.log 2>&1
STAMP=$(grep -o '\./backups/[0-9T]*Z' /tmp/verify-backup.log | head -1)
test -f "$STAMP/db.dump" && test -f "$STAMP/manifest.txt" && test -n "$(ls "$STAMP/blobs" 2>/dev/null)" && pass "backup $STAMP" || fail "backup incomplete"
echo "$STAMP" > /tmp/verify-stamp.txt

echo "ALL VERIFY CHECKS PASSED"
