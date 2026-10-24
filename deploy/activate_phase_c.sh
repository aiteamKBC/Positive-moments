#!/usr/bin/env bash
# =============================================================================================
# Phase C (deploy/DELIVERY_ROLLOUT.md): switch the running Continuous Bunny Publisher from
# STAGING_WRITE to PRODUCTION_WRITE on the SAME Bunny library 786045. No re-import, no Bunny
# setting change, no database write by this script, CDN token authentication stays DEFERRED.
#
#   sudo bash deploy/activate_phase_c.sh check     # pre-flight only: changes nothing, restarts nothing
#   sudo bash deploy/activate_phase_c.sh apply     # pre-flight, then swap publisher.env and restart
#                                                  # bunny-publisher.service ONLY, then verify
#   sudo bash deploy/activate_phase_c.sh verify    # read-only post-restart checks (re-runnable)
#
# How it stays safe
#   * The new configuration is built as a CANDIDATE file next to publisher.env and every gate runs
#     against the candidate: validate-config --check-library, security-check with the caption
#     read-back, and an independent HTTP 200 check of one existing caption URL. The live file is
#     untouched until all gates pass; then it is replaced with one atomic rename(2).
#   * Every existing line, secret and comment is kept byte for byte; only the nine Phase C keys
#     are set (the BUNNY_PRODUCTION_* values are copied from the BUNNY_STAGING_* lines of the same
#     file, never printed). A re-run produces the same file (idempotent) and says so.
#   * A root-only (0700 dir / 0600 file) timestamped backup is taken before the swap.
#   * The only service this script ever restarts is bunny-publisher.service (a readonly constant).
#     QA units (positive-moments, -scheduler, -backfill, -media) are only READ, and their start
#     timestamps are compared before/after to prove they were not restarted.
#   * The seven existing videos are adopted, not re-imported: rows are keyed by bunny_library_id,
#     not by mode. Pre-flight requires all seven present, VERIFIED, HIDDEN, with verified
#     captions; post-restart verify requires the same record IDs, the same caption publications,
#     no new IMPORT_VIDEO / PUBLISH_CAPTION job for their lectures and no release event.
#   * Nothing secret is printed: no key, database URL, CDN host, token or full video GUID.
#
# Rollback (printed again at the end of apply)
#   install -m 0640 -o root -g www-data <backup> /var/www/Positive-moments/backend/config/publisher.env
#   systemctl restart --no-block bunny-publisher.service
#   Restoring the file alone does NOTHING to the running daemon: systemd reads EnvironmentFile only
#   at start, so the publisher stays in PRODUCTION_WRITE until it is restarted. No data rollback is
#   needed: STAGING_WRITE and PRODUCTION_WRITE on 786045 read and write the same rows.
# =============================================================================================
set -Eeuo pipefail
umask 077

readonly APP=/var/www/Positive-moments
readonly ENV_FILE=$APP/backend/config/publisher.env
readonly DJANGO_ENV=$APP/backend/config/.env
readonly UNIT=bunny-publisher.service
readonly QA_UNITS="positive-moments.service positive-moments-scheduler.service positive-moments-backfill.service positive-moments-media.service"
readonly LIB=786045
readonly CODE_COMMIT=4ef80af            # the reviewed publisher code; HEAD may only add deploy/docs on top
readonly CAPTION_PROBE=069df826         # one existing verified video (prefix), for the caption checks
readonly BASELINE="010900eb 069df826 a1a8fc1d 456d51cf 04fe964d 550c9c30 51920222"
readonly VENV_PY=$APP/backend/venv/bin/python
readonly TS=$(date -u +%Y%m%dT%H%M%SZ)
readonly WORK=/root/kbc-phase-c/$TS
readonly CANDIDATE=$APP/backend/config/.publisher.env.phase-c-candidate
MODE=${1:-}

say()  { printf '\n== %s\n' "$*"; }
ROLLBACK_HINT=""
die()  { printf '\nABORT: %s\n' "$*" >&2; [ -z "$ROLLBACK_HINT" ] || printf '%s\n' "$ROLLBACK_HINT" >&2; exit 1; }
cli()  { sudo -u www-data "$VENV_PY" -m app.cli.publisher --env-file "$1" "${@:2}"; }

case "$MODE" in check|apply|verify) ;; *) echo "usage: sudo bash $0 check|apply|verify"; exit 64;; esac
[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
[ "$UNIT" = "bunny-publisher.service" ] || die "unit constant changed"
cd "$APP"
exec 9>/run/lock/kbc-phase-c.lock; flock -n 9 || die "another activate_phase_c.sh is running"
mkdir -p -m 0700 /root/kbc-phase-c "$WORK"
trap 'rm -f "$CANDIDATE"' EXIT

# ---------------------------------------------------------------------------------------------
# Read-only database checks (PUBLISHER_DATABASE_URL from the given env file, read-only session).
# ---------------------------------------------------------------------------------------------
db_state() {   # $1 env file, $2 baseline|verify, $3 restart time (UTC ISO) for verify, $4 before.json
  sudo -u www-data "$VENV_PY" - "$1" "$2" "${3:-}" "${4:-}" "$LIB" "$BASELINE" <<'PY'
import json, sys
from dotenv import dotenv_values
import psycopg
env_file, mode, since, before_file, lib, baseline = sys.argv[1:7]
lib, baseline = int(lib), baseline.split()
url = dotenv_values(env_file).get("PUBLISHER_DATABASE_URL") or ""
if not url: sys.exit("PUBLISHER_DATABASE_URL is empty")
conn = psycopg.connect(url, autocommit=True, prepare_threshold=None,
                       options="-c default_transaction_read_only=on")
assert conn.execute("SHOW transaction_read_only").fetchone()[0] == "on"
cur = psycopg.ClientCursor(conn)
def q(sql, *a):
    cur.execute(sql, a); return cur.fetchall()
videos = q("""SELECT bunny_video_record_id::text, bunny_video_id::text, lecture_id::text, import_status,
                     encoding_status, visibility_state, retired_at IS NOT NULL
              FROM bunny_videos WHERE bunny_library_id = %s""", lib)
base = [v for v in videos if v[1][:8] in baseline]
lectures = sorted({v[2] for v in base})
caps = q("""SELECT publication_id::text, bunny_video_id::text, publication_status, readback->>'content_check'
            FROM bunny_caption_publications WHERE bunny_library_id = %s AND superseded_at IS NULL""", lib)
base_caps = [c for c in caps if c[1][:8] in baseline]
state = {
    "videos_total": len(videos),
    "baseline_found": sorted(v[1][:8] for v in base),
    "baseline_records": sorted(v[0] for v in base),
    "baseline_ok": all(v[3] == "IDENTIFIED" and v[4] == "VERIFIED" and v[5] == "HIDDEN" and not v[6] for v in base),
    "visibility": sorted({v[5] for v in videos}),
    "baseline_captions": sorted(c[0] for c in base_caps),
    "baseline_captions_ok": len(base_caps) == len(baseline) and all(
        c[2] == "VERIFIED" and c[3] in ("IDENTICAL", "EQUIVALENT") for c in base_caps),
    "release_events": q("SELECT count(*) FROM bunny_publisher_events WHERE event IN "
                        "('PUBLICATION_RELEASED','PUBLICATION_WITHDRAWN')")[0][0],
    "fully_ready_events_baseline": q("SELECT count(*) FROM bunny_publisher_events WHERE event = 'LECTURE_FULLY_READY' "
                                     "AND lecture_id::text = ANY(%s)", lectures)[0][0],
    "active_jobs": q("SELECT count(*) FROM bunny_publisher_jobs WHERE status IN ('QUEUED','RUNNING','LEASED')")[0][0],
    "failed_jobs_48h": q("SELECT count(*) FROM bunny_publisher_jobs WHERE status = 'FAILED' "
                         "AND updated_at > now() - interval '48 hours'")[0][0],
}
problems = []
if sorted(state["baseline_found"]) != sorted(baseline): problems.append("BASELINE_VIDEOS_MISSING_OR_DUPLICATED")
if len(base) != len(baseline): problems.append("BASELINE_VIDEO_COUNT")
if not state["baseline_ok"]: problems.append("BASELINE_VIDEO_NOT_VERIFIED_HIDDEN")
if not state["baseline_captions_ok"]: problems.append("BASELINE_CAPTIONS_NOT_VERIFIED")
if state["visibility"] != ["HIDDEN"]: problems.append("A_VIDEO_IS_NOT_HIDDEN")
if state["release_events"]: problems.append("RELEASE_EVENTS_EXIST")
if mode == "verify":
    before = json.load(open(before_file)) if before_file else None
    beat = {r[0].rsplit(":", 1)[-1]: r for r in q(
        "SELECT worker_id, started_at >= %s::timestamptz, seen_at > now() - interval '120 seconds' "
        "FROM bunny_publisher_heartbeats WHERE started_at >= %s::timestamptz", since, since)}
    state["new_heartbeats"] = {k: bool(v[2]) for k, v in beat.items()}
    disc = q("SELECT last_started_at >= %s::timestamptz, last_status FROM bunny_publisher_schedule "
             "WHERE task = 'DISCOVERY'", since)
    state["discovery_since_restart"] = bool(disc and disc[0][0])
    state["discovery_status"] = disc[0][1] if disc else None
    state["reimport_jobs_since_restart"] = q(
        "SELECT count(*) FROM bunny_publisher_jobs WHERE job_kind IN ('IMPORT_VIDEO','PUBLISH_CAPTION') "
        "AND created_at >= %s::timestamptz AND lecture_id::text = ANY(%s)", since, lectures)[0][0]
    state["failed_jobs_since_restart"] = q("SELECT count(*) FROM bunny_publisher_jobs WHERE status = 'FAILED' "
                                          "AND updated_at >= %s::timestamptz", since)[0][0]
    for role in ("scheduler", "w0", "w1"):
        if not state["new_heartbeats"].get(role): problems.append(f"NO_FRESH_HEARTBEAT_{role}")
    if not state["discovery_since_restart"]: problems.append("DISCOVERY_NOT_YET_RUN_SINCE_RESTART")
    if state["reimport_jobs_since_restart"]: problems.append("REIMPORT_JOB_CREATED")
    if state["failed_jobs_since_restart"]: problems.append("FAILED_JOBS_SINCE_RESTART")
    if before:
        for key in ("baseline_records", "baseline_captions", "fully_ready_events_baseline"):
            if state[key] != before[key]: problems.append(f"CHANGED_{key.upper()}")
state["problems"] = problems
print(json.dumps(state))
PY
}

summary() {   # $1 json file: print counts, prefixes and problems only
  python3 - "$1" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
s = {k: (len(v) if k in ("baseline_records", "baseline_captions") else v) for k, v in s.items()}
print(json.dumps(s, indent=1))
PY
}

unit_since_utc() { date -u -d "$(TZ=UTC systemctl show -p ActiveEnterTimestamp --value "$UNIT")" +%Y-%m-%dT%H:%M:%SZ; }
startup_line() {   # the publisher's startup JSON line for the CURRENT process (journal saved first: no SIGPIPE)
  journalctl -u "$UNIT" --since "$(date -d "$1" '+%Y-%m-%d %H:%M:%S')" --no-pager -o cat > "$WORK/journal.txt" || true
  grep -m1 '"service": "kbc_bunny_publisher"' "$WORK/journal.txt" || true
}

unit_starts() { for u in $QA_UNITS; do printf '%s %s %s\n' "$u" "$(systemctl is-active "$u" || true)" \
                  "$(systemctl show -p ActiveEnterTimestampMonotonic --value "$u")"; done; }

# ---------------------------------------------------------------------------------------------
verify() {   # read-only; $1 = optional before.json
  local since; since=$(unit_since_utc)
  say "verify: $UNIT active since $since"
  systemctl is-active --quiet "$UNIT" || die "$UNIT is not active (journalctl -u $UNIT -n 50)"
  startup_line "$since" > "$WORK/startup.json"; [ -s "$WORK/startup.json" ] || die "no startup line in the journal yet"
  python3 - "$WORK/startup.json" "$LIB" <<'PY' || die "the daemon did not start in PRODUCTION_WRITE on 786045"
import json, sys
s = json.load(open(sys.argv[1]))
show = {k: s.get(k) for k in ("mode", "library_id", "adopts_staging_library", "cdn_token_auth_policy", "cdn_readback_signed")}
print("daemon started with:", json.dumps(show))
assert s["mode"] == "PRODUCTION_WRITE" and int(s["library_id"]) == int(sys.argv[2])
assert s["cdn_token_auth_policy"] == "DEFERRED" and s["cdn_readback_signed"] is False
PY
  local i out=$WORK/after.json
  for i in $(seq 1 14); do      # discovery is due immediately after start; give it up to 7 minutes
    db_state "$ENV_FILE" verify "$since" "${1:-}" > "$out"
    python3 -c 'import json,sys; sys.exit(0 if not json.load(open(sys.argv[1]))["problems"] else 1)' "$out" && break
    python3 -c 'import json,sys; p=json.load(open(sys.argv[1]))["problems"]; sys.exit(0 if set(p) <= {"DISCOVERY_NOT_YET_RUN_SINCE_RESTART","NO_FRESH_HEARTBEAT_scheduler","NO_FRESH_HEARTBEAT_w0","NO_FRESH_HEARTBEAT_w1"} else 1)' "$out" || break
    sleep 30
  done
  summary "$out"
  startup_line "$since" > /dev/null
  echo "journal error-like lines since restart: $(grep -ciE 'traceback|"refused"|error' "$WORK/journal.txt" || true)"
  python3 -c 'import json,sys; sys.exit(0 if not json.load(open(sys.argv[1]))["problems"] else 1)' "$out" \
    || die "post-restart verification found problems (above); see rollback in the script header"
  say "VERIFIED: PRODUCTION_WRITE on $LIB, scheduler + w0 + w1 fresh, discovery ran, baseline unchanged"
}

if [ "$MODE" = verify ]; then
  verify "$(ls -1d /root/kbc-phase-c/*/before.json 2>/dev/null | tail -1 || true)"; exit 0
fi

# ---------------------------------------------------------------------------------------------
say "1. pre-flight (read-only)"
git merge-base --is-ancestor "$CODE_COMMIT" HEAD || die "HEAD does not contain $CODE_COMMIT"
git diff --quiet "$CODE_COMMIT" HEAD -- app backend || die "publisher/Django code differs from the reviewed $CODE_COMMIT"
[ -z "$(git status --porcelain --untracked-files=no)" ] || die "tracked files are modified on the VPS"
echo "code: $(git rev-parse --short HEAD) on $(git branch --show-current) (publisher code == $CODE_COMMIT)"
systemctl show -p EnvironmentFiles --value "$UNIT" | grep -q "$ENV_FILE" || die "$UNIT does not read $ENV_FILE"
[ "$(systemctl show -p User --value "$UNIT")" = www-data ] || die "$UNIT does not run as www-data"
systemctl is-active --quiet "$UNIT" || die "$UNIT is not active now; investigate before switching modes"
[ -f "$ENV_FILE" ] || die "$ENV_FILE missing"
read -r OWNER GROUP PERM < <(stat -c '%U %G %a' "$ENV_FILE")
echo "publisher.env: $OWNER:$GROUP $PERM"
if [ -f "$DJANGO_ENV" ] && grep -qiE '^[[:space:]]*PUBLISHER_LMS_PLAYBACK_ENABLED[[:space:]]*=[[:space:]]*["'"'"']?(true|1|yes|on)' "$DJANGO_ENV"; then
  die "learner playback is enabled in the Django env; Phase C requires it off"
fi
unit_starts > "$WORK/qa_units.before"; cut -d' ' -f1,2 "$WORK/qa_units.before"
db_state "$ENV_FILE" baseline > "$WORK/before.json"; summary "$WORK/before.json"
python3 -c 'import json,sys; sys.exit(0 if not json.load(open(sys.argv[1]))["problems"] else 1)' "$WORK/before.json" \
  || die "baseline is not as expected (problems above); nothing changed"

say "2. build the candidate configuration (live file untouched)"
install -m 0640 -o root -g www-data /dev/null "$CANDIDATE"
python3 - "$ENV_FILE" "$CANDIDATE" "$LIB" <<'PY'
import re, sys
live, cand, lib = sys.argv[1], sys.argv[2], sys.argv[3]
raw = open(live, encoding="utf-8").read()
lines = raw.splitlines()
KEY = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
def parse(ls):
    out = {}
    for l in ls:
        m = KEY.match(l)
        if m: out.setdefault(m.group(1), []).append(m.group(2))
    return out
def unq(v): v = v.strip(); return v[1:-1] if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'" else v
kv = parse(lines)
last = {k: v[-1] for k, v in kv.items()}                    # systemd and dotenv: the last one wins
for k in ("BUNNY_STAGING_LIBRARY_ID", "BUNNY_STAGING_API_KEY", "BUNNY_STAGING_ACCOUNT_API_KEY", "BUNNY_STAGING_CDN_HOSTNAME"):
    if not unq(last.get(k, "")): sys.exit(f"ABORT: {k} is empty in publisher.env")
if unq(last["BUNNY_STAGING_LIBRARY_ID"]) != lib: sys.exit(f"ABORT: the staging library is not {lib}")
if unq(last.get("BUNNY_PRODUCTION_LIBRARY_ID", "")) not in ("", lib): sys.exit("ABORT: another production library is set")
for prod, stag in (("BUNNY_PRODUCTION_API_KEY", "BUNNY_STAGING_API_KEY"),
                   ("BUNNY_PRODUCTION_ACCOUNT_API_KEY", "BUNNY_STAGING_ACCOUNT_API_KEY"),
                   ("BUNNY_PRODUCTION_CDN_HOSTNAME", "BUNNY_STAGING_CDN_HOSTNAME")):
    if unq(last.get(prod, "")) not in ("", unq(last[stag])):
        sys.exit(f"ABORT: {prod} is already set to a DIFFERENT value than {stag}; not overwriting it")
want = {
    "PUBLISHER_MODE": "PRODUCTION_WRITE",
    "PUBLISHER_PRODUCTION_WRITE_ENABLED": "true",
    "BUNNY_PRODUCTION_LIBRARY_ID": lib,
    "BUNNY_PRODUCTION_API_KEY": last["BUNNY_STAGING_API_KEY"],             # verbatim, quotes included
    "BUNNY_PRODUCTION_ACCOUNT_API_KEY": last["BUNNY_STAGING_ACCOUNT_API_KEY"],
    "BUNNY_PRODUCTION_CDN_HOSTNAME": last["BUNNY_STAGING_CDN_HOSTNAME"],
    "PUBLISHER_CDN_TOKEN_AUTH": "DEFERRED",
    "PUBLISHER_APPROVED_REFERRERS": "lms.kentbusinesscollege.org",
    "PUBLISHER_REQUIRE_BLOCK_NONE_REFERRER": "true",
}
out, seen = [], set()
for l in lines:
    m = KEY.match(l)
    if m and m.group(1) in want:
        if m.group(1) in seen: continue                     # drop a duplicate of a key we set
        out.append(f"{m.group(1)}={want[m.group(1)]}"); seen.add(m.group(1))
    else:
        out.append(l)                                       # every other line byte for byte
missing = [k for k in want if k not in seen]
if missing: out += ["", "# Phase C (deploy/activate_phase_c.sh): production mode on the same library"] + \
                   [f"{k}={want[k]}" for k in missing]
text = "\n".join(out) + "\n"
# Self-check: every key we do not own keeps exactly its old value(s); ours appear once, as wanted.
new = parse(text.splitlines())
for k, v in kv.items():
    if k not in want and new.get(k) != v: sys.exit(f"ABORT: internal check failed for an untouched key ({k})")
for k, v in want.items():
    if new.get(k) != [v]: sys.exit(f"ABORT: internal check failed for {k}")
if set(new) - set(kv) - set(want): sys.exit("ABORT: unexpected new key")
open(cand, "w", encoding="utf-8").write(text)
print("keys preserved unchanged:", len([k for k in kv if k not in want]), "| keys set:", len(want),
      "| already applied:", text == raw)
if unq(last.get("BUNNY_CDN_TOKEN_KEY", "")): print("note: BUNNY_CDN_TOKEN_KEY is set; it is ignored while DEFERRED")
PY
if cmp -s "$CANDIDATE" "$ENV_FILE"; then ALREADY=1; echo "publisher.env already holds the Phase C settings"; else ALREADY=0; fi

say "3. validate-config --check-library on the candidate"
cli "$CANDIDATE" validate-config --check-library > "$WORK/validate.json" || { summary "$WORK/validate.json" 2>/dev/null || cat "$WORK/validate.json"; die "validate-config failed; live file untouched"; }
python3 - "$WORK/validate.json" "$LIB" <<'PY' || die "validate-config answer not as expected; live file untouched"
import json, sys
r = json.load(open(sys.argv[1])); c = r["config"]
keys = ("mode", "writes_bunny", "library_id", "adopts_staging_library", "cdn_readback_configured", "cdn_token_auth_policy",
        "cdn_readback_signed", "cdn_token_key_ignored", "approved_referrers", "require_block_none_referrer")
print(json.dumps({**{k: c.get(k) for k in keys}, "problems": r.get("problems"), "library_problems": r.get("library_problems")}))
assert c["mode"] == "PRODUCTION_WRITE" and c["writes_bunny"] is True and int(c["library_id"]) == int(sys.argv[2])
assert c["adopts_staging_library"] is True and c["cdn_readback_configured"] is True
assert c["cdn_token_auth_policy"] == "DEFERRED" and c["cdn_readback_signed"] is False
assert c["approved_referrers"] in (["lms.kentbusinesscollege.org"], ("lms.kentbusinesscollege.org",))
assert not r["problems"] and r.get("library_problems") == []
PY

say "4. security-check (read-only) with the caption read-back, on the candidate"
GUID=$(sudo -u www-data "$VENV_PY" - "$ENV_FILE" "$LIB" "$CAPTION_PROBE" <<'PY'
import sys, psycopg
from dotenv import dotenv_values
with psycopg.connect(dotenv_values(sys.argv[1])["PUBLISHER_DATABASE_URL"], prepare_threshold=None,
                     options="-c default_transaction_read_only=on") as c:
    rows = c.execute("SELECT bunny_video_id::text FROM bunny_videos WHERE bunny_library_id = %s "
                     "AND bunny_video_id::text LIKE %s", (int(sys.argv[2]), sys.argv[3] + "%")).fetchall()
print(rows[0][0] if len(rows) == 1 else "")
PY
)
[ -n "$GUID" ] || die "caption probe video not found"
cli "$CANDIDATE" security-check --caption-readback-video-id "$GUID" > "$WORK/security.json" \
  || { cat "$WORK/security.json"; die "library 786045 is not secure under the approved policy; live file untouched"; }
python3 - "$WORK/security.json" <<'PY' || die "security-check answer not as expected; live file untouched"
import json, sys
r = json.load(open(sys.argv[1])); cb = r.get("caption_readback") or {}; cdn = r.get("cdn_token_authentication") or {}
print(json.dumps({"secure": r.get("secure"), "problems": r.get("problems"), "cdn_token_authentication": cdn,
                  "warnings": [w.split(":")[0] for w in r.get("warnings", [])],
                  "caption_readback": {k: cb.get(k) for k in ("status", "signed", "is_webvtt")}}))
assert r["secure"] is True and r["problems"] == []
assert cdn == {"policy": "DEFERRED", "status": "DEFERRED", "enabled": False}
assert cb.get("status") == 200 and cb.get("signed") is False and cb.get("is_webvtt") is True
PY

say "5. independent HTTP check of one existing caption URL (status only; must be 200)"
CAP_STATUS=$(python3 - "$CANDIDATE" "$GUID" <<'PY' | curl -s -o /dev/null -w '%{http_code}' --proto '=https' --max-time 20 -K - || true
import re, sys
host = ""
for l in open(sys.argv[1], encoding="utf-8"):
    m = re.match(r"^\s*BUNNY_PRODUCTION_CDN_HOSTNAME\s*=(.*)$", l)
    if m: host = m.group(1).strip().strip("\"'")
print(f'url = "https://{host}/{sys.argv[2]}/captions/en.vtt"')
PY
)
echo "caption $CAPTION_PROBE…/captions/en.vtt -> HTTP $CAP_STATUS"
[ "$CAP_STATUS" = 200 ] || die "the existing caption did not answer 200; live file untouched"

if [ "$MODE" = check ]; then
  say "CHECK PASSED. Nothing changed, nothing restarted. Run: sudo bash deploy/activate_phase_c.sh apply"; exit 0
fi

running_mode() {   # the mode the CURRENT daemon process started with (its startup line)
  startup_line "$(unit_since_utc)" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("mode",""))' || true
}
if [ "$ALREADY" = 1 ] && [ "$(running_mode)" = PRODUCTION_WRITE ]; then
  say "already applied and the running daemon is already in PRODUCTION_WRITE: no backup, no restart"
  verify "$(ls -1d /root/kbc-phase-c/*/before.json 2>/dev/null | head -1 || true)"; exit 0
fi

say "6. backup (root-only) and atomic swap"
BACKUP=/root/kbc-phase-c/publisher.env.before-phase-c.$TS
cp --preserve=mode,ownership,timestamps "$ENV_FILE" "$BACKUP"; chown root:root "$BACKUP"; chmod 0600 "$BACKUP"
cmp -s "$ENV_FILE" "$BACKUP" || die "backup copy differs"
printf '%s:%s %s\n' "$OWNER" "$GROUP" "$PERM" > "$BACKUP.stat"
echo "backup: $BACKUP (root:root 0600)"
RESTORE_FROM=$BACKUP
if [ "$ALREADY" = 1 ]; then   # an earlier run already swapped the file: roll back to the oldest (staging) backup
  RESTORE_FROM=$(ls -1 /root/kbc-phase-c/publisher.env.before-phase-c.* 2>/dev/null | grep -v '\.stat$' | head -1)
  grep -qE '^PUBLISHER_MODE=PRODUCTION_WRITE$' "$RESTORE_FROM" && RESTORE_FROM="(no STAGING_WRITE backup found in /root/kbc-phase-c)"
fi
ROLLBACK_HINT="ROLLBACK (two steps; restoring the file alone changes nothing until the restart):
  install -m $PERM -o $OWNER -g $GROUP $RESTORE_FROM $ENV_FILE
  systemctl restart --no-block $UNIT"
if [ "$ALREADY" = 0 ]; then
  chown "$OWNER:$GROUP" "$CANDIDATE"; chmod "$PERM" "$CANDIDATE"; sync -f "$CANDIDATE"
  mv -f "$CANDIDATE" "$ENV_FILE"                       # rename(2) in the same directory: atomic
  sync -f "$ENV_FILE"
fi
stat -c 'publisher.env now: %U:%G %a' "$ENV_FILE"
grep -cE '^PUBLISHER_MODE=PRODUCTION_WRITE$' "$ENV_FILE" | grep -qx 1 || die "swap not visible in $ENV_FILE"

say "7. graceful restart of $UNIT ONLY (SIGTERM, TimeoutStopSec=180)"
PREV_START=$(systemctl show -p ActiveEnterTimestampMonotonic --value "$UNIT")
systemctl restart --no-block "$UNIT"
for i in $(seq 1 60); do       # up to 5 min: stop (<= 180 s) + ExecStartPre validate-config + start
  s=$(systemctl is-active "$UNIT" || true)
  [ "$s" = active ] && [ "$(systemctl show -p ActiveEnterTimestampMonotonic --value "$UNIT")" != "$PREV_START" ] && break
  [ "$s" = failed ] && break
  sleep 5
done
if [ "$(systemctl is-active "$UNIT" || true)" != active ] \
   || [ "$(systemctl show -p ActiveEnterTimestampMonotonic --value "$UNIT")" = "$PREV_START" ]; then
  echo "!! $UNIT did not become active: restoring the backup and restarting it on the previous configuration"
  [ -f "$RESTORE_FROM" ] || die "no STAGING_WRITE backup to restore automatically"
  install -m "$PERM" -o "$OWNER" -g "$GROUP" "$RESTORE_FROM" "$ENV_FILE.rollback.tmp"; mv -f "$ENV_FILE.rollback.tmp" "$ENV_FILE"
  systemctl reset-failed "$UNIT" || true; systemctl restart --no-block "$UNIT"
  ROLLBACK_HINT=""; die "rolled back to $RESTORE_FROM; see journalctl -u $UNIT -n 80"
fi
sleep 20
verify "$WORK/before.json"

say "8. QA services untouched"
unit_starts > "$WORK/qa_units.after"; cut -d' ' -f1,2 "$WORK/qa_units.after"
cmp -s "$WORK/qa_units.before" "$WORK/qa_units.after" && echo "QA units: same state and same start time as before" \
  || echo "WARNING: a QA unit changed state/start time during the run (this script never touches them); check it"

printf '\nDONE. Evidence (root-only): %s\n%s\n' "$WORK" "$ROLLBACK_HINT"
