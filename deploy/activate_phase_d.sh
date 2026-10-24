#!/usr/bin/env bash
# =============================================================================================
# Phase D for LMS integration testing (deploy/DELIVERY_ROLLOUT.md): release EXACTLY the seven
# verified lectures through the supported, audited `release` command and switch the Publisher
# API's playback flag on, then verify the public HTTPS API.
#
#   sudo bash deploy/activate_phase_d.sh check                  # read-only pre-flight
#   sudo bash deploy/activate_phase_d.sh apply ["Actor name"]   # release the 7, set the flag, restart Django ONLY
#   sudo bash deploy/activate_phase_d.sh verify                 # read-only public API verification
#
# This changes only: bunny_videos.visibility_state of those seven lectures' videos (HIDDEN ->
# READY_FOR_LMS, with one PUBLICATION_RELEASED audit event each) and PUBLISHER_LMS_PLAYBACK_ENABLED
# in the Django env file. No Bunny call writes anything; no video, caption or ID changes;
# bunny-publisher.service and the QA units are not restarted. `release` itself refuses unless the
# lecture is verified right now and library 786045 passes the live learner-delivery security check.
#
# playback.available=true means "the Publisher has a released, verified recording". It is NOT a
# learner entitlement: the LMS must still authenticate the learner and check enrolment before it
# signs an embed URL.
#
# Rollback (each is enough on its own):
#   * per lecture, immediate, no restart:
#     sudo -u www-data backend/venv/bin/python -m app.cli.publisher --env-file backend/config/publisher.env \
#          withdraw --lecture-id <uuid> --actor "<name>" --reason "<why>"
#   * all at once: restore the Django env backup printed by apply (or set the flag to false), then
#     systemctl restart --no-block positive-moments.service   (the file alone changes nothing until then)
# =============================================================================================
set -Eeuo pipefail
umask 077
readonly APP=/var/www/Positive-moments
readonly PUB_ENV=$APP/backend/config/publisher.env
readonly DJANGO_ENV=$APP/backend/config/.env
readonly WEB_UNIT=positive-moments.service
readonly LIB=786045
readonly CODE_COMMIT=4ef80af
readonly BASE=https://positive-moments.kentbusinesscollege.net/api/publisher/v1
readonly HTTP_BASE=http://positive-moments.kentbusinesscollege.net/api/publisher/v1
readonly VENV_PY=$APP/backend/venv/bin/python
readonly TS=$(date -u +%Y%m%dT%H%M%SZ)
readonly WORK=/root/kbc-phase-d/$TS
# occurrence -> expected Bunny video id prefix (the seven verified lectures; nothing else is touched)
readonly SEVEN="OCC-4493AC65DF5041459BC5A14DA8B357A0=a1a8fc1d OCC-188B392FF4ED45A9B46C02E40072A1F9=550c9c30
OCC-774CB73AB0094A03A13F87E30B9FF4C0=456d51cf OCC-EB064C5DE7B44E119E67F128896C592B=010900eb
OCC-5FDA481E5B1145398AA108E04D19F9A4=51920222 OCC-075C8136DD8546FD834CC6B0E4B7DE18=069df826
OCC-B8C49F1C2ED4429786178B5914ED7368=04fe964d"
MODE=${1:-}
ACTOR=${2:-"KBC operator (deploy/activate_phase_d.sh)"}
readonly REASON="LMS integration testing: release of the seven verified lectures of 23 Oct 2026"
ROLLBACK_HINT=""
die() { printf '\nABORT: %s\n' "$*" >&2; [ -z "$ROLLBACK_HINT" ] || printf '%s\n' "$ROLLBACK_HINT" >&2; exit 1; }
say() { printf '\n== %s\n' "$*"; }
cli() { sudo -u www-data "$VENV_PY" -m app.cli.publisher --env-file "$PUB_ENV" "$@" < /dev/null; }
case "$MODE" in check|apply|verify) ;; *) echo "usage: sudo bash $0 check|apply [actor]|verify"; exit 64;; esac
[ "$(id -u)" -eq 0 ] || die "run as root"
cd "$APP"
exec 9>/run/lock/kbc-phase-d.lock; flock -n 9 || die "another activate_phase_d.sh is running"
mkdir -p -m 0700 /root/kbc-phase-d "$WORK"
AUTH=$(mktemp); trap 'shred -u "$AUTH" 2>/dev/null || rm -f "$AUTH"' EXIT

# The service token goes into a 0600 temp file as a curl header file; it is never echoed, never in argv.
python3 - "$DJANGO_ENV" > "$AUTH" <<'PY'
import re, sys
tok = ""
for l in open(sys.argv[1], encoding="utf-8"):
    m = re.match(r"\s*PUBLISHER_LMS_SERVICE_TOKENS\s*=\s*(.*)$", l)
    if m: tok = m.group(1).strip().strip("\"'").split(",")[0].strip()
if len(tok) < 32: sys.exit("ABORT: no usable PUBLISHER_LMS_SERVICE_TOKENS in the Django env")
print("Authorization: Bearer " + tok)
PY

# ------------------------------------------------------------------- public API verification
api_verify() {   # $1 = expected: RELEASED|NOT_RELEASED ; $2 = expected playback true|false
  local f=$WORK/api.$1.txt; : > "$f"
  for pair in $SEVEN; do
    occ=${pair%%=*}
    code=$(curl -s -o "$WORK/occ.json" -w '%{http_code}' --proto '=https' --max-time 30 -H @"$AUTH" "$BASE/occurrences/$occ/recording" || true)
    printf '%s %s %s\n' "$occ" "${pair#*=}" "$code" >> "$f"; cat "$WORK/occ.json" >> "$f"; echo >> "$f"
  done
  printf 'unauth %s\n' "$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$BASE/health")" >> "$f"
  printf 'unknown %s\n' "$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -H @"$AUTH" "$BASE/occurrences/OCC-DOES-NOT-EXIST/recording")" >> "$f"
  printf 'health %s\n' "$(curl -s --max-time 20 -H @"$AUTH" "$BASE/health" | tr -d '\n')" >> "$f"
  # plain HTTP WITHOUT a token: it must be refused before Django (no X-Publisher-Contract header)
  printf 'http %s %s\n' "$(curl -s -o /dev/null -D "$WORK/http.h" -w '%{http_code}' --max-time 20 "$HTTP_BASE/health")" \
         "$(grep -ci '^x-publisher-contract:' "$WORK/http.h" || true)" >> "$f"
  python3 - "$AUTH" "$BASE" > "$WORK/feed.json" <<'PY' || true
import json, sys, urllib.parse, urllib.request
name, value = open(sys.argv[1]).read().strip().split(": ", 1)
items, cursor = [], None
for _ in range(200):
    q = {"limit": 50, **({"cursor": cursor} if cursor else {})}
    req = urllib.request.Request(f"{sys.argv[2]}/occurrences?{urllib.parse.urlencode(q)}", headers={name: value})
    page = json.load(urllib.request.urlopen(req, timeout=120))
    items += page["items"]; cursor = page.get("next_cursor")
    if not page.get("has_more"): break
print(json.dumps({"items": items, "has_more": bool(page.get("has_more"))}))
PY
  python3 - "$f" "$WORK/feed.json" "$1" "$2" "$LIB" <<'PY'
import json, sys
path, feed_path, want_release, want_play, lib = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "true", int(sys.argv[5])
lines = open(path).read().splitlines()
fails, seven = [], set()
def need(ok, what):
    print(("PASS " if ok else "FAIL ") + what); ok or fails.append(what)
i = 0
while i < len(lines) and lines[i].startswith("OCC-"):
    occ, prefix, code = lines[i].split(); seven.add(occ)
    try: a = json.loads(lines[i + 1])
    except Exception: a = {}
    rec = a.get("recording") or {}; parts = rec.get("parts") or []
    vids = [p["bunny"]["video_id"][:8] for p in parts]
    libs = {p["bunny"]["library_id"] for p in parts}
    caps = bool(parts) and all(any(c["language"] == "en" and c["status"] == "VERIFIED" and c["content_verified"]
                                   for c in p["captions"]) for p in parts)
    pb = a.get("playback") or {}
    ok = (code == "200" and a.get("state") == "READY" and (a.get("release") or {}).get("status") == want_release
          and pb.get("available") is want_play and pb.get("library_id") == lib and libs == {lib}
          and vids == [prefix] and rec.get("published_part_count") == 1 and caps)
    need(ok, f"{occ[:12]}… {code} {a.get('state')} {(a.get('release') or {}).get('status')} available={pb.get('available')} "
             f"reason={pb.get('reason')} library={sorted(libs)} video={vids} parts={rec.get('published_part_count')} en_captions_verified={caps}")
    i += 2
rest = dict(l.split(" ", 1) for l in lines[i:])
need(rest.get("unauth") == "401", f"unauthenticated -> {rest.get('unauth')} (401)")
need(rest.get("unknown") == "404", f"unknown occurrence -> {rest.get('unknown')} (404)")
h = json.loads(rest.get("health", "{}") or "{}")
need(h.get("library_id") == lib and h.get("playback_enabled") is want_play,
     f"health library={h.get('library_id')} playback_enabled={h.get('playback_enabled')}")
code, contract = (rest.get("http", "? ?").split() + ["?"])[:2]
need(contract == "0" and code in ("403", "400", "426"),
     f"plain-HTTP API refused before Django -> {code}, contract header {contract} (needs harden_api_https.sh)")
try:
    feed = json.load(open(feed_path)); items = feed.get("items", [])
    available = {x["lms_occurrence_id"] for x in items if (x.get("playback") or {}).get("available")}
    need(not feed.get("has_more") and available <= seven and (available == seven) is want_play,
         f"whole feed: {len({x['lms_occurrence_id'] for x in items})} occurrences, {len(available)} available, "
         f"none outside the seven")
except Exception as e:
    need(False, f"feed unreadable ({type(e).__name__})")
sys.exit(1 if fails else 0)
PY
}

if [ "$MODE" = verify ]; then
  api_verify RELEASED true && say "VERIFIED" || die "verification failed (above); evidence $WORK"; exit 0
fi

# ------------------------------------------------------------------- pre-flight (read-only)
say "1. pre-flight (read-only)"
git merge-base --is-ancestor "$CODE_COMMIT" HEAD || die "HEAD does not contain $CODE_COMMIT"
git diff --quiet "$CODE_COMMIT" HEAD -- app backend || die "publisher/Django code differs from the reviewed $CODE_COMMIT"
systemctl show -p EnvironmentFiles --value "$WEB_UNIT" | grep -q "$DJANGO_ENV" || die "$WEB_UNIT does not read $DJANGO_ENV"
systemctl is-active --quiet "$WEB_UNIT" || die "$WEB_UNIT is not active"
systemctl is-active --quiet bunny-publisher.service || die "bunny-publisher.service is not active"
grep -qE '^PUBLISHER_MODE=PRODUCTION_WRITE$' "$PUB_ENV" || die "the publisher is not configured for PRODUCTION_WRITE"
grep -qE "^PUBLISHER_LMS_LIBRARY_ID=$LIB\$" "$DJANGO_ENV" || die "PUBLISHER_LMS_LIBRARY_ID is not $LIB in the Django env"
grep -qE '^PUBLISHER_CDN_TOKEN_AUTH=DEFERRED$' "$PUB_ENV" || die "PUBLISHER_CDN_TOKEN_AUTH is not DEFERRED"

cli security-check > "$WORK/security.json" || { cat "$WORK/security.json"; die "library $LIB is not secure under the approved policy"; }
python3 - "$WORK/security.json" <<'PY' || die "security-check answer not as expected"
import json, sys
r = json.load(open(sys.argv[1])); o = r.get("observed") or {}
print(json.dumps({"secure": r["secure"], "problems": r["problems"], "cdn": r.get("cdn_token_authentication"),
                  "embed_token": o.get("PlayerTokenAuthenticationEnabled"), "direct_play": o.get("AllowDirectPlay"),
                  "referrers": o.get("AllowedReferrers"), "block_no_referrer": o.get("BlockNoneReferrer")}))
assert r["secure"] is True and r["problems"] == []
assert r["cdn_token_authentication"] == {"policy": "DEFERRED", "status": "DEFERRED", "enabled": False}
PY

: > "$WORK/lectures.txt"
for pair in $SEVEN; do
  occ=${pair%%=*}; prefix=${pair#*=}
  cli delivery --occurrence-id "$occ" > "$WORK/$occ.json" || die "$occ: delivery lookup failed"
  python3 - "$WORK/$occ.json" "$prefix" "$LIB" >> "$WORK/lectures.txt" <<'PY' || die "pre-flight failed for an occurrence (above)"
import json, sys
a = json.load(open(sys.argv[1])); prefix, lib = sys.argv[2], int(sys.argv[3])
rec = a.get("recording") or {}; parts = rec.get("parts") or []
vids = [p["bunny"]["video_id"][:8] for p in parts]
caps = bool(parts) and all(any(c["language"] == "en" and c["status"] == "VERIFIED" and c["content_verified"]
                               for c in p["captions"]) for p in parts)
ok = (a.get("state") == "READY" and a.get("blocking") == [] and vids == [prefix]
      and all(p["encoding"] == "VERIFIED" and p["bunny"]["library_id"] == lib for p in parts)
      and rec.get("published_part_count") == 1 and caps
      and a["release"]["status"] in ("NOT_RELEASED", "RELEASED"))
print(a["lecture"]["lecture_id"], a["lms_occurrence_id"], a["release"]["status"])
print(f"{'OK  ' if ok else 'FAIL'} {a['lms_occurrence_id'][:12]}… {a.get('state')} video={vids} parts={rec.get('published_part_count')} "
      f"captions={caps} release={a['release']['status']} lecture={a['lecture']['lecture_id'][:8]}…", file=sys.stderr)
sys.exit(0 if ok else 1)
PY
done
[ "$(cut -d' ' -f1 "$WORK/lectures.txt" | sort -u | wc -l)" = 7 ] || die "the seven occurrences do not map to seven distinct lectures"

sudo -u www-data "$VENV_PY" - "$PUB_ENV" "$LIB" "$WORK/lectures.txt" <<'PY' || die "inventory check failed (above)"
import sys, psycopg
from dotenv import dotenv_values
env, lib, lect = sys.argv[1], int(sys.argv[2]), [l.split()[0] for l in open(sys.argv[3])]
with psycopg.connect(dotenv_values(env)["PUBLISHER_DATABASE_URL"], prepare_threshold=None,
                     options="-c default_transaction_read_only=on") as c:
    rows = c.execute("SELECT lecture_id::text, recording_part_index, bunny_video_id::text, visibility_state "
                     "FROM bunny_videos WHERE bunny_library_id = %s AND retired_at IS NULL", (lib,)).fetchall()
    dup = c.execute("SELECT count(*) FROM (SELECT lecture_id, recording_part_index FROM bunny_videos WHERE bunny_library_id = %s "
                    "AND retired_at IS NULL GROUP BY 1, 2 HAVING count(*) > 1) d", (lib,)).fetchone()[0]
    others_released = [r for r in rows if r[0] not in lect and r[3] != "HIDDEN"]
print(f"videos in {lib}: {len(rows)} | of the seven lectures: {sum(r[0] in lect for r in rows)} | other lectures: "
      f"{sum(r[0] not in lect for r in rows)} | duplicate parts: {dup} | other lectures not HIDDEN: {len(others_released)}")
assert sum(r[0] in lect for r in rows) == 7 and dup == 0 and not others_released
PY
HTTP_ANSWER="$(curl -s -o /dev/null -D "$WORK/http.h" -w '%{http_code}' --max-time 20 "$HTTP_BASE/health" || true) $(grep -ci '^x-publisher-contract:' "$WORK/http.h" || true)"
if [ "${HTTP_ANSWER#* }" != 0 ]; then
  [ "$MODE" = apply ] && die "plain HTTP still reaches the Publisher API ($HTTP_ANSWER): run deploy/harden_api_https.sh apply first"
  echo "NOTE: plain HTTP still reaches the Publisher API; apply will refuse until deploy/harden_api_https.sh apply has run"
else
  echo "plain-HTTP Publisher API refused by the web server: ${HTTP_ANSWER% *}"
fi
if [ "$MODE" = check ]; then say "CHECK PASSED. Nothing changed. Next: sudo bash deploy/activate_phase_d.sh apply \"<your name>\""; exit 0; fi

# ------------------------------------------------------------------- release the seven (audited, idempotent)
say "2. release the seven lectures (actor: $ACTOR)"
ROLLBACK_HINT="ROLLBACK: withdraw each released lecture (immediate, no restart):
$(cut -d' ' -f1 "$WORK/lectures.txt" | sed "s|^|  sudo -u www-data $VENV_PY -m app.cli.publisher --env-file $PUB_ENV withdraw --actor \"$ACTOR\" --reason \"rollback of phase D\" --lecture-id |")"
while read -r lecture occ _ <&3; do
  cli release --lecture-id "$lecture" --actor "$ACTOR" --reason "$REASON" > "$WORK/release.$occ.json" \
    || { cat "$WORK/release.$occ.json"; die "release refused for $occ (nothing else released after it)"; }
  python3 - "$WORK/release.$occ.json" "$occ" <<'PY' || die "release answer not as expected for $occ"
import json, sys
r = json.load(open(sys.argv[1]))
print(f"{sys.argv[2][:12]}… release={r['release']['status']} changed={r['changed']} parts={r['parts_changed']} state={r['state']}")
assert r["release"]["status"] == "RELEASED" and r["state"] == "READY"
PY
done 3< "$WORK/lectures.txt"

# ------------------------------------------------------------------- playback flag (Django env, atomic)
say "3. PUBLISHER_LMS_PLAYBACK_ENABLED=true in the Django env"
read -r OWNER GROUP PERM < <(stat -c '%U %G %a' "$DJANGO_ENV")
BACKUP=/root/kbc-phase-d/django.env.before-phase-d.$TS
cp --preserve=mode,ownership,timestamps "$DJANGO_ENV" "$BACKUP"; chown root:root "$BACKUP"; chmod 0600 "$BACKUP"
cmp -s "$DJANGO_ENV" "$BACKUP" || die "backup differs"
CAND=$(dirname "$DJANGO_ENV")/.env.phase-d-candidate
install -m "$PERM" -o "$OWNER" -g "$GROUP" /dev/null "$CAND"
python3 - "$DJANGO_ENV" "$CAND" <<'PY'
import re, sys
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
KEY = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
out, seen = [], False
for l in lines:
    m = KEY.match(l)
    if m and m.group(1) == "PUBLISHER_LMS_PLAYBACK_ENABLED":
        if not seen: out.append("PUBLISHER_LMS_PLAYBACK_ENABLED=true"); seen = True
    else:
        out.append(l)
if not seen: out.append("PUBLISHER_LMS_PLAYBACK_ENABLED=true")
old = {m.group(1): m.group(2) for m in map(KEY.match, lines) if m}
new = {m.group(1): m.group(2) for m in map(KEY.match, out) if m}
assert {k: v for k, v in old.items() if k != "PUBLISHER_LMS_PLAYBACK_ENABLED"} == \
       {k: v for k, v in new.items() if k != "PUBLISHER_LMS_PLAYBACK_ENABLED"}, "an unrelated key changed"
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(out) + "\n")
print("keys kept unchanged:", len(new) - 1)
PY
mv -f "$CAND" "$DJANGO_ENV"
echo "backup: $BACKUP (root:root 0600)"
ROLLBACK_HINT="$ROLLBACK_HINT
or switch playback off for everything (two steps; the file alone changes nothing until the restart):
  install -m $PERM -o $OWNER -g $GROUP $BACKUP $DJANGO_ENV
  systemctl restart --no-block $WEB_UNIT"

say "4. restart $WEB_UNIT ONLY"
PREV=$(systemctl show -p ActiveEnterTimestampMonotonic --value "$WEB_UNIT")
QA_BEFORE=$(for u in bunny-publisher positive-moments-scheduler positive-moments-backfill positive-moments-media; do systemctl show -p ActiveEnterTimestampMonotonic --value $u.service; done)
systemctl restart --no-block "$WEB_UNIT"
for i in $(seq 1 36); do
  [ "$(systemctl is-active "$WEB_UNIT" || true)" = active ] && [ "$(systemctl show -p ActiveEnterTimestampMonotonic --value "$WEB_UNIT")" != "$PREV" ] \
    && curl -s -o /dev/null -w '%{http_code}' --max-time 10 -H @"$AUTH" "$BASE/health" | grep -qx 200 && break
  sleep 5
done
[ "$(systemctl is-active "$WEB_UNIT" || true)" = active ] || die "$WEB_UNIT did not come back (journalctl -u $WEB_UNIT -n 80)"
QA_AFTER=$(for u in bunny-publisher positive-moments-scheduler positive-moments-backfill positive-moments-media; do systemctl show -p ActiveEnterTimestampMonotonic --value $u.service; done)
[ "$QA_BEFORE" = "$QA_AFTER" ] && echo "bunny-publisher and QA units: not restarted" || echo "WARNING: another unit's start time changed during the run"

say "5. public HTTPS API verification"
api_verify RELEASED true || die "post-change verification failed (above); evidence $WORK"
printf '\nDONE. Evidence (root-only): %s\n%s\n' "$WORK" "$ROLLBACK_HINT"
