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
# occurrence = expected Bunny video id prefix : expected lecture id prefix (the seven verified lectures;
# nothing else is touched). Read back from production on 24 Oct 2026.
readonly SEVEN="OCC-4493AC65DF5041459BC5A14DA8B357A0=a1a8fc1d:0fe6c2fa OCC-188B392FF4ED45A9B46C02E40072A1F9=550c9c30:15e61dca
OCC-774CB73AB0094A03A13F87E30B9FF4C0=456d51cf:3f09b150 OCC-EB064C5DE7B44E119E67F128896C592B=010900eb:95a710b0
OCC-5FDA481E5B1145398AA108E04D19F9A4=51920222:a7c5fb2a OCC-075C8136DD8546FD834CC6B0E4B7DE18=069df826:b4761fb4
OCC-B8C49F1C2ED4429786178B5914ED7368=04fe964d:d2768b20"
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
    rest=${pair#*=}
    printf '%s %s %s\n' "$occ" "${rest%%:*}" "$code" >> "$f"; cat "$WORK/occ.json" >> "$f"; echo >> "$f"
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

# One line per occurrence in $WORK/lectures.txt: "<lecture uuid> <occurrence> <RELEASED|NOT_RELEASED>".
# Read from the `delivery` answer (the v1 occurrence shape: release is {"status", "released_at",
# "withdrawn_at"}). A RELEASED occurrence (e.g. left by an interrupted run) is accepted only when it is
# fully consistent: expected lecture and video, READY, verified captions, every part READY_FOR_LMS,
# a release time and no withdrawal. Anything else stops the script.
occurrence_state() {   # $1 occurrence, $2 video prefix, $3 lecture prefix, $4 required release ("" = either)
  cli delivery --occurrence-id "$1" > "$WORK/$1.json" || die "$1: delivery lookup failed"
  python3 - "$WORK/$1.json" "$1" "$2" "$3" "$LIB" "${4:-}" <<'PY' || die "occurrence state not as expected (above)"
import json, sys
path, occ, vprefix, lprefix, lib, required = sys.argv[1:4] + sys.argv[4:5] + [int(sys.argv[5])] + sys.argv[6:7]
a = json.load(open(path))
def get(d, *keys):
    for k in keys:
        d = d.get(k) if isinstance(d, dict) else None
    return d
rec = get(a, "recording") or {}
parts = rec.get("parts") if isinstance(rec.get("parts"), list) else []
lecture = str(get(a, "lecture", "lecture_id") or "")
release = get(a, "release", "status")
vids = [str(get(p, "bunny", "video_id") or "")[:8] for p in parts]
vis = [get(p, "visibility") for p in parts]
caps = bool(parts) and all(any(get(c, "language") == "en" and get(c, "status") == "VERIFIED" and get(c, "content_verified") is True
                               for c in (get(p, "captions") or [])) for p in parts)
problems = []
if get(a, "lms_occurrence_id") != occ: problems.append("OCCURRENCE_MISMATCH")
if not (lecture and lecture.startswith(lprefix)): problems.append("UNEXPECTED_LECTURE")
if get(a, "state") != "READY" or get(a, "blocking") != []: problems.append(f"STATE_{get(a, 'state')}")
if vids != [vprefix] or get(rec, "published_part_count") != 1: problems.append("UNEXPECTED_VIDEO_OR_PARTS")
if not all(get(p, "encoding") == "VERIFIED" and get(p, "bunny", "library_id") == lib for p in parts): problems.append("VIDEO_NOT_VERIFIED")
if not caps: problems.append("CAPTIONS_NOT_VERIFIED")
if release == "RELEASED":
    if vis != ["READY_FOR_LMS"] * len(parts) or not get(a, "release", "released_at") or get(a, "release", "withdrawn_at"):
        problems.append("INCONSISTENT_RELEASED_STATE")
elif release == "NOT_RELEASED":
    if vis != ["HIDDEN"] * len(parts): problems.append("INCONSISTENT_NOT_RELEASED_STATE")
else:
    problems.append(f"RELEASE_{release}")
if required and release != required: problems.append(f"EXPECTED_{required}")
print(f"{'OK  ' if not problems else 'FAIL'} {occ[:12]}… {get(a, 'state')} video={vids} parts={get(rec, 'published_part_count')} "
      f"captions={caps} release={release} visibility={vis} lecture={lecture[:8]}… {' '.join(problems)}", file=sys.stderr)
if problems: sys.exit(1)
print(lecture, occ, release)
PY
}

# Read-only database confirmation, as www-data with ids as arguments (never a root-only path).
# $1 = the seven lecture ids, $2 = the ids that must be RELEASED now. Checks the persisted state, not a
# CLI message: one live video per lecture, visibility READY_FOR_LMS exactly for $2 and HIDDEN otherwise,
# exactly ONE PUBLICATION_RELEASED and no PUBLICATION_WITHDRAWN per released lecture, none elsewhere.
db_check() {
  sudo -u www-data "$VENV_PY" - "$PUB_ENV" "$LIB" "$1" "${2:-}" <<'PY' || die "database check failed (above)"
import sys, psycopg
from dotenv import dotenv_values
env, lib, lect, rel = sys.argv[1], int(sys.argv[2]), sys.argv[3].split(), set(sys.argv[4].split())
if len(set(lect)) != 7 or not rel <= set(lect): sys.exit("expected 7 distinct lectures, the released ones among them")
with psycopg.connect(dotenv_values(env)["PUBLISHER_DATABASE_URL"], prepare_threshold=None,
                     options="-c default_transaction_read_only=on") as c:
    rows = c.execute("SELECT lecture_id::text, recording_part_index, bunny_video_id::text, visibility_state "
                     "FROM bunny_videos WHERE bunny_library_id = %s AND retired_at IS NULL", (lib,)).fetchall()
    dup = c.execute("SELECT count(*) FROM (SELECT lecture_id, recording_part_index FROM bunny_videos WHERE bunny_library_id = %s "
                    "AND retired_at IS NULL GROUP BY 1, 2 HAVING count(*) > 1) d", (lib,)).fetchone()[0]
    events = c.execute("SELECT lecture_id::text, event, count(*) FROM bunny_publisher_events WHERE event IN "
                       "('PUBLICATION_RELEASED', 'PUBLICATION_WITHDRAWN') GROUP BY 1, 2").fetchall()
ev = {(l, e): n for l, e, n in events}
mine = [r for r in rows if r[0] in lect]
problems = []
if len(mine) != 7 or len({r[0] for r in mine}) != 7: problems.append("SEVEN_LECTURES_NOT_ONE_VIDEO_EACH")
if dup: problems.append("DUPLICATE_PARTS")
if any(r[3] != "HIDDEN" for r in rows if r[0] not in lect): problems.append("ANOTHER_LECTURE_NOT_HIDDEN")
for r in mine:
    if r[3] != ("READY_FOR_LMS" if r[0] in rel else "HIDDEN"): problems.append(f"VISIBILITY_{r[0][:8]}_{r[3]}")
for l in lect:
    n_rel, n_wd = ev.get((l, "PUBLICATION_RELEASED"), 0), ev.get((l, "PUBLICATION_WITHDRAWN"), 0)
    if n_wd or n_rel != (1 if l in rel else 0): problems.append(f"EVENTS_{l[:8]}_released{n_rel}_withdrawn{n_wd}")
if any(l not in lect for (l, _e) in ev): problems.append("LIFECYCLE_EVENTS_OUTSIDE_THE_SEVEN")
released_events = sum(n for (_l, e), n in ev.items() if e == "PUBLICATION_RELEASED")
withdrawals = sum(n for (_l, e), n in ev.items() if e == "PUBLICATION_WITHDRAWN")
print(f"database: videos in {lib}: {len(rows)} | seven lectures: {len(mine)} | READY_FOR_LMS: "
      f"{sum(r[3] == 'READY_FOR_LMS' for r in mine)} (expected {len(rel)}) | release events: {released_events} | "
      f"withdrawals: {withdrawals} | duplicate parts: {dup} | {' '.join(problems) or 'consistent'}")
sys.exit(1 if problems else 0)
PY
}

prefixes_of() { local pair; for pair in $SEVEN; do [ "${pair%%=*}" = "$1" ] && { echo "${pair#*=}"; return 0; }; done; return 1; }

: > "$WORK/lectures.txt"
for pair in $SEVEN; do
  occ=${pair%%=*}; rest=${pair#*=}
  occurrence_state "$occ" "${rest%%:*}" "${rest#*:}" >> "$WORK/lectures.txt"
done
[ "$(cut -d' ' -f1 "$WORK/lectures.txt" | sort -u | wc -l)" = 7 ] || die "the seven occurrences do not map to seven distinct lectures"
LECTURE_IDS=$(cut -d' ' -f1 "$WORK/lectures.txt" | tr '\n' ' ')
RELEASED_IDS=$(awk '$3 == "RELEASED" {print $1}' "$WORK/lectures.txt" | tr '\n' ' ')
echo "release state: $(awk '$3 == "RELEASED"' "$WORK/lectures.txt" | wc -l) of 7 already RELEASED (accepted only because consistent)"
db_check "$LECTURE_IDS" "$RELEASED_IDS"
HTTP_ANSWER="$(curl -s -o /dev/null -D "$WORK/http.h" -w '%{http_code}' --max-time 20 "$HTTP_BASE/health" || true) $(grep -ci '^x-publisher-contract:' "$WORK/http.h" || true)"
if [ "${HTTP_ANSWER#* }" != 0 ]; then
  [ "$MODE" = apply ] && die "plain HTTP still reaches the Publisher API ($HTTP_ANSWER): run deploy/harden_api_https.sh apply first"
  echo "NOTE: plain HTTP still reaches the Publisher API; apply will refuse until deploy/harden_api_https.sh apply has run"
else
  echo "plain-HTTP Publisher API refused by the web server: ${HTTP_ANSWER% *}"
fi
if [ "$MODE" = check ]; then say "CHECK PASSED. Nothing changed. Next: sudo bash deploy/activate_phase_d.sh apply \"<your name>\""; exit 0; fi

# ------------------------------------------------------------------- release the remaining lectures (audited)
say "2. release the lectures that are not yet RELEASED (actor: $ACTOR)"
ROLLBACK_HINT="ROLLBACK is manual and explicit; this script never withdraws anything by itself.
Withdraw a released lecture (immediate, no restart):
$(cut -d' ' -f1 "$WORK/lectures.txt" | sed "s|^|  sudo -u www-data $VENV_PY -m app.cli.publisher --env-file $PUB_ENV withdraw --actor \"<name>\" --reason \"<why>\" --lecture-id |")"
while read -r lecture occ status <&3; do
  rest=$(prefixes_of "$occ") || die "$occ is not one of the seven"
  if [ "$status" = RELEASED ]; then
    echo "${occ:0:12}… already RELEASED and consistent: not released again (no new audit event)"
    continue
  fi
  cli release --lecture-id "$lecture" --actor "$ACTOR" --reason "$REASON" > "$WORK/release.$occ.json" \
    || { cat "$WORK/release.$occ.json"; die "release refused for $occ; nothing after it was released, playback flag unchanged"; }
  # `release` prints delivery.release(): {"lecture_id", "library_id", "action": "RELEASE", "changed": bool,
  # "parts_changed": [...], "release": "RELEASED" (a STRING), "state": "READY" (a STRING), "blocking": [...]}
  python3 - "$WORK/release.$occ.json" "$occ" "$lecture" "$LIB" <<'PY' || die "release answer not as expected for $occ; playback flag unchanged"
import json, sys
r = json.load(open(sys.argv[1])); occ, lecture, lib = sys.argv[2], sys.argv[3], int(sys.argv[4])
if not isinstance(r, dict): sys.exit("release printed no JSON object")
print(f"{occ[:12]}… release={r.get('release')!r} state={r.get('state')!r} changed={r.get('changed')} parts={r.get('parts_changed')}")
assert r.get("lecture_id") == lecture and r.get("library_id") == lib and r.get("action") == "RELEASE", "identity"
assert r.get("release") == "RELEASED" and r.get("state") == "READY", "release/state"
PY
  # Confirm the PERSISTED state, not the CLI message: the live answer and the database rows/events.
  occurrence_state "$occ" "${rest%%:*}" "${rest#*:}" RELEASED > /dev/null
  RELEASED_IDS="$RELEASED_IDS $lecture"
  db_check "$LECTURE_IDS" "$RELEASED_IDS"
done 3< "$WORK/lectures.txt"

say "2b. all seven independently confirmed before the playback flag is touched"
db_check "$LECTURE_IDS" "$LECTURE_IDS"
RUNNING_FLAG=$(curl -s --proto '=https' --max-time 20 -H @"$AUTH" "$BASE/health" \
  | python3 -c 'import json,sys; print("true" if json.load(sys.stdin).get("playback_enabled") is True else "false")') \
  || die "health unreadable"
api_verify RELEASED "$RUNNING_FLAG" || die "public API does not show all seven RELEASED; playback flag NOT changed"

# ------------------------------------------------------------------- playback flag (Django env, atomic)
say "3. PUBLISHER_LMS_PLAYBACK_ENABLED=true in the Django env"
if [ "$RUNNING_FLAG" = true ] && grep -qx 'PUBLISHER_LMS_PLAYBACK_ENABLED=true' "$DJANGO_ENV"; then
  echo "already on in the file and in the running service: no edit, no restart"
  say "5. public HTTPS API verification"
  api_verify RELEASED true || die "verification failed (above); evidence $WORK"
  printf '\nDONE. Evidence (root-only): %s\n%s\n' "$WORK" "$ROLLBACK_HINT"; exit 0
fi
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
