#!/usr/bin/env bash
# =============================================================================================
# Turn on the publisher's AUTO_RELEASE task in production (deploy/DELIVERY_ROLLOUT.md, "Automatic
# release of new lectures"). No date restriction: live and historical (backfilled) lectures alike.
# Idempotent; every mode can be repeated.
#
#   sudo bash deploy/activate_auto_release.sh preview                    # read-only: EVERY lecture the first runs would
#                                                                        # release (all dates), its count and fingerprint
#   sudo bash deploy/activate_auto_release.sh apply <count> <fingerprint> # only if the eligible set is still exactly the
#                                                                        # reviewed one; restart bunny-publisher ONLY; verify
#   sudo bash deploy/activate_auto_release.sh verify                     # read-only post-activation verification
#   sudo bash deploy/activate_auto_release.sh rollback                   # PUBLISHER_AUTO_RELEASE=false, restart publisher ONLY
#
# apply sets PUBLISHER_AUTO_RELEASE=true in backend/config/publisher.env and removes a leftover
# PUBLISHER_AUTO_RELEASE_FROM line (no longer used). Every other line (credentials included) is kept
# byte for byte, and the script asserts it. The live file is replaced atomically (rename in the same
# directory, same owner and mode) only after the candidate file passed `validate-config
# --check-library`, the library passed `security-check`, the eligible set equals the reviewed preview,
# and the seven released lectures are intact.
#
# Only bunny-publisher.service is restarted. positive-moments.service (Django API) and the QA units
# are never touched. No secret, key or connection string is printed; evidence goes to a root-only
# directory. Lectures are shown by lecture id, date, LMS occurrence and title; Bunny video ids by
# prefix only.
#
# Never withdraws anything. If activation fails after the file changed, apply rolls the
# CONFIGURATION back (PUBLISHER_AUTO_RELEASE=false) and restarts the publisher only; anything the
# task released stays released and is listed with the operator's per-lecture withdraw command.
# =============================================================================================
set -Eeuo pipefail
umask 077
readonly APP=/var/www/Positive-moments
readonly PUB_ENV=$APP/backend/config/publisher.env
readonly UNIT=bunny-publisher.service
readonly OTHER_UNITS="positive-moments.service positive-moments-scheduler.service positive-moments-backfill.service positive-moments-media.service"
readonly LIB=786045
readonly CODE_COMMIT=3c66741
readonly VENV_PY=$APP/backend/venv/bin/python
readonly STATE=/root/kbc-auto-release
readonly TS=$(date -u +%Y%m%dT%H%M%SZ)
readonly WORK=$STATE/$TS
# The seven lectures released in Phase D: lecture id prefix:Bunny video id prefix (read back 24 Oct 2026).
readonly SEVEN="0fe6c2fa:a1a8fc1d 15e61dca:550c9c30 3f09b150:456d51cf 95a710b0:010900eb a7c5fb2a:51920222 b4761fb4:069df826 d2768b20:04fe964d"
readonly ORPHAN=be785a53            # Bunny video with no publisher record: must stay unknown to the publisher
FIRST_RUN_TIMEOUT=1200              # seconds to wait for the first AUTO_RELEASE run after the restart
MODE=${1:-}; [ "$MODE" = check ] && MODE=preview
REVIEWED_COUNT=${2:-}; REVIEWED_PRINT=${3:-}
CAND=""
die() { printf '\nABORT: %s\n' "$*" >&2; exit 1; }
say() { printf '\n== %s\n' "$*"; }
cli() { sudo -u www-data "$VENV_PY" -m app.cli.publisher "$@" < /dev/null; }
case "$MODE" in preview|apply|verify|rollback) ;; *) echo "usage: sudo bash $0 preview | apply <count> <fingerprint> | verify | rollback"; exit 64;; esac
if [ "$MODE" = apply ] && ! { [[ "$REVIEWED_COUNT" =~ ^[0-9]+$ ]] && [[ "$REVIEWED_PRINT" =~ ^[0-9a-f]{12}$ ]]; }; then
  echo "apply needs the count and fingerprint printed by: sudo bash $0 preview"; exit 64
fi
[ "$(id -u)" -eq 0 ] || die "run as root"
cd "$APP"
exec 9>/run/lock/kbc-auto-release.lock; flock -n 9 || die "another activate_auto_release.sh is running"
mkdir -p -m 0700 "$STATE" "$WORK"
trap '[ -z "$CAND" ] || rm -f "$CAND"' EXIT

# ------------------------------------------------------------------- read-only database probe
# Runs as www-data (it reads the env file itself; nothing root-only is passed, only small arguments).
#   probe candidates ENV                     EVERY lecture AUTO_RELEASE would release now (its own CANDIDATES +
#                                            ineligible(), all dates), with count and fingerprint of that set
#   probe snapshot ENV                       the seven intact, nothing else released, no duplicates, orphan unknown
#   probe runtime ENV PID SINCE MAXEV IDS    the restarted service: heartbeats, first AUTO_RELEASE run, new
#                                            events/imports (IDS = the reviewed eligible lecture ids)
# Exit 0 = fine, 1 = problem, 3 = not yet (runtime only).
probe() {
  sudo -u www-data env PYTHONPATH="$APP" "$VENV_PY" - "$@" "$LIB" "$SEVEN" "$ORPHAN" <<'PY'
import hashlib, json, sys
from collections import Counter, defaultdict
from datetime import datetime
import psycopg
from app.publisher.config import DB_CONNECT_KWARGS, PublisherConfig
ACTOR = "publisher:auto-release"
action, env = sys.argv[1], sys.argv[2]
extra = sys.argv[3:-3]
lib, seven_pairs, orphan = int(sys.argv[-3]), sys.argv[-2], sys.argv[-1]
SEVEN = dict(p.split(":") for p in seven_pairs.split())
config = PublisherConfig.from_environment(env)
problems, warnings = [], []
def need(ok, what):
    ok or problems.append(what)
    return ok
p8 = lambda x: (x or "")[:8]

with psycopg.connect(config.database_url, prepare_threshold=None, **DB_CONNECT_KWARGS) as c:
    c.read_only = True
    if action == "candidates":
        from app.publisher import auto_release
        from app.publisher import auto_release, delivery
        need(config.auto_release, "CANDIDATE_CONFIG_NOT_AUTO_RELEASE")
        # Exactly the task's own selection and per-lecture rule (newest first), with no limit: the whole set.
        ids = [r[0] for r in c.execute(auto_release.CANDIDATES, {"library": lib}).fetchall()]
        why = {l: auto_release.ineligible(c, l, library_id=lib, language=config.caption_language) for l in ids}
        eligible = [l for l in ids if why[l] is None]
        print(f"AUTO_RELEASE candidates (every part HIDDEN + VERIFIED, current LMS link, match VERIFIED, never "
              f"released/withdrawn), all dates: {len(ids)}")
        skipped = Counter(w for w in why.values() if w)
        for reason, n in sorted(skipped.items()):
            print(f"  not eligible: {n} x {reason}: {' '.join(p8(l) for l in ids if why[l] == reason)}")
        print(f"\nELIGIBLE: these {len(eligible)} lecture(s) would be released by the first runs "
              f"(at most {auto_release.MAX_PER_RUN} per 5-minute run, newest first):")
        print("  lecture_id                            date        occurrence                              parts captions       video   title")
        by_date = Counter()
        for l in eligible:
            subject, day, occ = c.execute(
                "SELECT l.subject, l.session_date, (SELECT k.lms_occurrence_id FROM bunny_lms_occurrence_links k "
                "WHERE k.lecture_id = l.lecture_id AND k.is_current LIMIT 1) FROM lecture_sessions l "
                "WHERE l.lecture_id = %s", (l,)).fetchone()
            view = delivery.lecture_delivery(c, l, library_id=lib, language=config.caption_language)
            vids = ",".join(p8((p.get("bunny") or {}).get("video_id")) for p in view["parts"])
            by_date[day.strftime("%Y-%m")] += 1
            print(f"  {l}  {day}  {occ or '-':38}  {len(view['parts']):5} {view['caption_status']:14} {vids:7} "
                  f"{(subject or '')[:60]}")
        if by_date:
            print("  by month: " + ", ".join(f"{m}: {n}" for m, n in sorted(by_date.items())))
        waiting = c.execute("""
            SELECT count(DISTINCT v.lecture_id) FROM bunny_videos v
             WHERE v.bunny_library_id = %s AND v.retired_at IS NULL AND v.visibility_state = 'HIDDEN'
               AND v.encoding_status <> 'VERIFIED'""", (lib,)).fetchone()[0]
        print(f"\nstill in the pipeline (a hidden video not yet verified; may become eligible later): {waiting} lecture(s)")
        fingerprint = hashlib.sha256(" ".join(sorted(eligible)).encode()).hexdigest()[:12]
        print(json.dumps({"eligible_count": len(eligible), "fingerprint": fingerprint, "candidates": len(ids),
                          "eligible_ids": sorted(eligible)}))
    else:
        db_now = c.execute("SELECT now()").fetchone()[0]
        rows = c.execute("""
            SELECT v.lecture_id::text, v.recording_part_index, v.bunny_video_id::text, v.visibility_state,
                   v.encoding_status, v.import_status, v.created_at, l.session_date,
                   EXISTS (SELECT 1 FROM bunny_lms_occurrence_links k WHERE k.lecture_id = v.lecture_id AND k.is_current),
                   EXISTS (SELECT 1 FROM bunny_lms_match_evaluations m WHERE m.lecture_id = v.lecture_id AND m.is_current
                                                                         AND m.match_status = 'VERIFIED')
              FROM bunny_videos v JOIN lecture_sessions l ON l.lecture_id = v.lecture_id
             WHERE v.bunny_library_id = %s AND v.retired_at IS NULL""", (lib,)).fetchall()
        events = c.execute("""
            SELECT event_id, lecture_id::text, event, actor FROM bunny_publisher_events
             WHERE event IN ('PUBLICATION_RELEASED', 'PUBLICATION_WITHDRAWN') ORDER BY event_id""").fetchall()
        dup = c.execute("SELECT count(*) FROM (SELECT 1 FROM bunny_videos WHERE bunny_library_id = %s AND retired_at IS NULL "
                        "GROUP BY lecture_id, recording_part_index HAVING count(*) > 1) d", (lib,)).fetchone()[0]
        orphan_rows = c.execute("SELECT count(*) FROM bunny_videos WHERE bunny_video_id::text LIKE %s",
                                (orphan + "%",)).fetchone()[0]
        max_event = max((e[0] for e in events), default=0)
        by_lecture = defaultdict(list)
        for r in rows:
            by_lecture[r[0]].append(r)
        seq = defaultdict(list)
        for e in events:
            seq[e[1]].append(e[2])
        # The seven: one live video each, the expected video, released, verified, one release event, no withdrawal.
        seven_ids = set()
        for lp, vp in SEVEN.items():
            match = [l for l in by_lecture if l.startswith(lp)]
            if not need(len(match) == 1, f"SEVEN_{lp}_NOT_FOUND"):
                continue
            l = match[0]; seven_ids.add(l); mine = by_lecture[l]
            need(len(mine) == 1 and (mine[0][2] or "").startswith(vp) and mine[0][3] == "READY_FOR_LMS"
                 and mine[0][4] == "VERIFIED", f"SEVEN_{lp}_VIDEO_CHANGED")
            need(seq[l] == ["PUBLICATION_RELEASED"], f"SEVEN_{lp}_EVENTS_{'+'.join(seq[l]) or 'NONE'}")
        need(dup == 0, f"DUPLICATE_LIVE_PARTS:{dup}")
        need(orphan_rows == 0, "ORPHAN_VIDEO_HAS_A_PUBLISHER_RECORD")
        for l, s in seq.items():          # a release must never follow a release without a withdrawal between
            need(all(not (a == b == "PUBLICATION_RELEASED") for a, b in zip(s, s[1:])), f"DUPLICATE_RELEASE_{p8(l)}")
        released_now = {l for l, rs in by_lecture.items() if any(r[3] != "HIDDEN" for r in rs)}
        others = released_now - seven_ids
        summary = {"db_now": db_now.isoformat(), "max_event_id": max_event, "videos": len(rows),
                   "released_lectures": len(released_now), "seven_ok": len(seven_ids) == len(SEVEN) and not problems,
                   "duplicate_parts": dup, "release_events": sum(e[2] == "PUBLICATION_RELEASED" for e in events),
                   "withdraw_events": sum(e[2] == "PUBLICATION_WITHDRAWN" for e in events)}
        if action == "snapshot":
            need(not others, f"OTHER_LECTURES_RELEASED:{' '.join(sorted(p8(l) for l in others))}")
        elif action == "runtime":
            pid, since, base_event = extra[0], datetime.fromisoformat(extra[1]), int(extra[2])
            reviewed = set(extra[3].split()) if len(extra) > 3 else set()
            new = [e for e in events if e[0] > base_event]
            auto = sorted({e[1] for e in new if e[2] == "PUBLICATION_RELEASED" and e[3] == ACTOR})
            for e in new:
                if e[2] == "PUBLICATION_WITHDRAWN":
                    (problems if e[3].startswith("publisher") else warnings).append(f"NEW_WITHDRAWAL_{p8(e[1])}_by_{e[3]}")
                elif e[3] != ACTOR:
                    warnings.append(f"OPERATOR_RELEASE_{p8(e[1])}_by_{e[3]}")
            for l in auto:                 # each automatic release must satisfy the rule, checked again here
                rs = by_lecture.get(l, [])
                need(l not in seven_ids and seq[l] == ["PUBLICATION_RELEASED"] and rs
                     and all(r[4] == "VERIFIED" and r[3] == "READY_FOR_LMS" for r in rs)
                     and rs[0][8] and rs[0][9], f"AUTO_RELEASE_RULE_BROKEN_{p8(l)}")
                if l not in reviewed:      # became eligible after the preview (e.g. today's lecture): allowed, reported
                    warnings.append(f"RELEASED_AFTER_PREVIEW_{p8(l)}_{rs[0][7] if rs else ''}")
            known = {e[1] for e in new if e[2] == "PUBLICATION_RELEASED"}
            need(not (others - known), f"RELEASED_WITHOUT_NEW_EVENT:{' '.join(sorted(p8(l) for l in others - known))}")
            imports = [r for r in rows if r[6] >= since]
            for r in imports:
                need(r[0] in auto or r[3] == "HIDDEN", f"NEW_IMPORT_VISIBLE_{p8(r[0])}")
            summary.update(auto_released=len(auto), auto_released_reviewed=len(set(auto) & reviewed),
                           auto_released_ids=[p8(l) for l in auto], new_lifecycle_events=len(new),
                           new_imports=[f"{p8(r[0])}.../part{r[1]} {r[5]}/{r[4]}/{r[3]}" for r in imports])
            # The restarted service: scheduler + every worker heartbeating under the NEW pid.
            hb = c.execute("SELECT worker_id, extract(epoch FROM now() - seen_at), detail FROM bunny_publisher_heartbeats "
                           "WHERE worker_id LIKE %s", (f"%:{pid}:%",)).fetchall()
            names = {w.rsplit(":", 1)[-1]: (age, (d or {}).get("stopped")) for w, age, d in hb}
            want = ["scheduler"] + [f"w{n}" for n in range(config.workers)]
            healthy = all(n in names and names[n][0] < 300 and not names[n][1] for n in want)
            summary["heartbeats"] = {n: (round(names[n][0]) if n in names else None) for n in want}
            row = c.execute("SELECT last_started_at, last_finished_at, last_succeeded_at, last_status, last_detail "
                            "FROM bunny_publisher_schedule WHERE task = 'AUTO_RELEASE'").fetchone()
            disc = c.execute("SELECT last_status, last_succeeded_at FROM bunny_publisher_schedule WHERE task = 'DISCOVERY'").fetchone()
            ran = row is not None and row[0] is not None and row[0] >= since and row[3] in ("SUCCEEDED", "FAILED")
            summary["auto_release_task"] = None if row is None else {
                "last_started_at": row[0].isoformat() if row[0] else None, "status": row[3],
                "detail": {k: v for k, v in (row[4] or {}).items() if k in (
                    "candidates", "eligible", "released", "unchanged", "deferred", "skipped", "refused", "error", "code")}}
            summary["discovery_task"] = None if disc is None else {"status": disc[0],
                                                                   "succeeded_at": disc[1].isoformat() if disc[1] else None}
            if ran:
                need(row[3] == "SUCCEEDED", f"AUTO_RELEASE_TASK_{row[3]}_{(row[4] or {}).get('code')}")
            if not healthy:
                summary["pending"] = "workers"
            elif not ran:
                summary["pending"] = "first AUTO_RELEASE run"
        print(json.dumps(summary, default=str))
for w in warnings:
    print("WARNING " + w)
if problems:
    print("PROBLEMS " + " ".join(problems))
    sys.exit(1)
sys.exit(3 if action == "runtime" and "pending" in summary else 0)
PY
}

# ------------------------------------------------------------------- env file editing (atomic)
# write_candidate SRC DEST KEY=VALUE... [-KEY...]: DEST gets SRC with each KEY set (first line replaced,
# later duplicates dropped, appended if absent) and each -KEY line removed; every other line is asserted
# identical. Owner and mode of SRC.
write_candidate() {
  local src=$1 dest=$2; shift 2
  local owner group perm; read -r owner group perm < <(stat -c '%U %G %a' "$src")
  install -m "$perm" -o "$owner" -g "$group" /dev/null "$dest"
  python3 - "$src" "$dest" "$@" <<'PY'
import re, sys
src, dest = sys.argv[1], sys.argv[2]
sets = dict(a.split("=", 1) for a in sys.argv[3:] if not a.startswith("-"))
drop = {a[1:] for a in sys.argv[3:] if a.startswith("-")}
KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
lines = open(src, encoding="utf-8").read().splitlines()
out, done = [], set()
for l in lines:
    m = KEY.match(l)
    if m and m.group(1) in drop:
        continue
    if m and m.group(1) in sets:
        if m.group(1) not in done:
            out.append(f"{m.group(1)}={sets[m.group(1)]}"); done.add(m.group(1))
        continue
    out.append(l)
missing = [k for k in sets if k not in done]
if missing:
    out += ["", "# Automatic release (deploy/activate_auto_release.sh)"] + [f"{k}={sets[k]}" for k in missing]
keep = lambda ls: [l for l in ls if not ((m := KEY.match(l)) and m.group(1) in set(sets) | drop)
                   and l != "# Automatic release (deploy/activate_auto_release.sh)"]
assert [l for l in keep(lines) if l.strip()] == [l for l in keep(out) if l.strip()], "an unrelated line changed"
open(dest, "w", encoding="utf-8").write("\n".join(out) + "\n")
print(f"candidate: {len(lines) - len(keep(lines))} line(s) replaced or removed, {len(missing)} appended, "
      f"{len([l for l in keep(lines) if l.strip()])} other non-empty lines unchanged")
PY
}

validate() {   # $1 env file, $2 expected auto_release true|false
  cli --env-file "$1" validate-config --check-library > "$WORK/validate.$2.json" \
    || { python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print({"problems": r.get("problems"), "library_problems": r.get("library_problems")})' "$WORK/validate.$2.json" || true
         return 1; }
  python3 - "$WORK/validate.$2.json" "$2" "$LIB" <<'PY'
import json, sys
r = json.load(open(sys.argv[1])); c = r["config"]; on = sys.argv[2] == "true"
print(json.dumps({k: c.get(k) for k in ("mode", "library_id", "auto_release", "cdn_token_auth_policy",
                                         "workers")} | {"problems": r["problems"], "library_problems": r.get("library_problems")}))
assert r["problems"] == [] and r.get("library_problems") == [], "configuration problems"
assert c["mode"] == "PRODUCTION_WRITE" and c["library_id"] == int(sys.argv[3]), "mode/library"
assert c["cdn_token_auth_policy"] == "DEFERRED", "CDN token authentication must stay DEFERRED"
assert c["auto_release"] is on, "auto_release"
assert "auto_release_from" not in c, "the deployed code still has the date restriction"
PY
}

security() {
  cli --env-file "$PUB_ENV" security-check > "$WORK/security.json" || { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("problems"))' "$WORK/security.json" || true; return 1; }
  python3 - "$WORK/security.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1])); o = r.get("observed") or {}
print(json.dumps({"secure": r["secure"], "problems": r["problems"], "cdn": r.get("cdn_token_authentication"),
                  "embed_token": o.get("PlayerTokenAuthenticationEnabled"), "direct_play": o.get("AllowDirectPlay"),
                  "block_no_referrer": o.get("BlockNoneReferrer")}))
assert r["secure"] is True and r["problems"] == []
assert r["cdn_token_authentication"] == {"policy": "DEFERRED", "status": "DEFERRED", "enabled": False}
PY
}

main_pid() { systemctl show -p MainPID --value "$UNIT"; }
started_utc() {   # $1 pid: when that process started, UTC (from its elapsed seconds)
  date -u -d "@$(( $(date +%s) - $(ps -o etimes= -p "$1" | tr -d ' ') ))" +%Y-%m-%dT%H:%M:%S+00:00; }
unit_marks() { local u; for u in $OTHER_UNITS; do printf '%s=%s ' "$u" "$(systemctl show -p ActiveEnterTimestampMonotonic --value "$u")"; done; }

restart_publisher() {   # restart ONLY bunny-publisher; prints the new MainPID
  local old; old=$(main_pid)
  systemctl restart --no-block "$UNIT"
  for _ in $(seq 1 60); do
    sleep 5
    local pid; pid=$(main_pid)
    [ "$(systemctl is-active "$UNIT" || true)" = active ] && [ "$pid" != 0 ] && [ "$pid" != "$old" ] && { echo "$pid"; return 0; }
    [ "$(systemctl is-active "$UNIT" || true)" = failed ] && break
  done
  return 1
}

wait_runtime() {   # $1 pid, $2 since, $3 baseline event id, $4 reviewed ids; polls the probe until done or timeout
  local deadline=$(( $(date +%s) + FIRST_RUN_TIMEOUT )) rc
  while :; do
    set +e; probe runtime "$PUB_ENV" "$1" "$2" "$3" "${4:-}" > "$WORK/runtime.txt" 2> "$WORK/runtime.err"; rc=$?; set -e
    [ "$rc" != 3 ] && break
    [ "$(systemctl is-active "$UNIT" || true)" = active ] && [ "$(main_pid)" = "$1" ] || { rc=1; echo "service not running as pid $1" >> "$WORK/runtime.txt"; break; }
    [ "$(date +%s)" -lt "$deadline" ] || { echo "TIMEOUT waiting for $(python3 -c 'import json,sys; print(json.loads(open(sys.argv[1]).readline()).get("pending"))' "$WORK/runtime.txt" 2>/dev/null)" >> "$WORK/runtime.txt"; rc=1; break; }
    sleep 20
  done
  cat "$WORK/runtime.txt"
  return "$rc"
}

journal_errors() {   # $1 since: count ERROR lines (content not printed: it may name lectures; read journalctl yourself)
  local n; n=$(journalctl -u "$UNIT" --since "$(date -d "$1" '+%F %T')" --no-pager -o cat 2>/dev/null | grep -c ' ERROR ' || true)
  echo "journal ERROR lines since restart: $n   (journalctl -u $UNIT --since '$(date -d "$1" '+%F %T')')"
}

rollback_config() {   # PUBLISHER_AUTO_RELEASE=false (everything else kept), restart the publisher only. Never withdraws.
  say "ROLLBACK: PUBLISHER_AUTO_RELEASE=false, restart $UNIT only (nothing is withdrawn)"
  CAND=$(dirname "$PUB_ENV")/.publisher.env.rollback.$TS
  write_candidate "$PUB_ENV" "$CAND" PUBLISHER_AUTO_RELEASE=false -PUBLISHER_AUTO_RELEASE_FROM
  local rpid; rpid=$(main_pid)
  if cmp -s "$CAND" "$PUB_ENV" && [ "${rpid:-0}" != 0 ] && systemctl is-active --quiet "$UNIT" \
     && [ "$(date -d "$(started_utc "$rpid")" +%s)" -ge "$(stat -c %Y "$PUB_ENV")" ]; then
    rm -f "$CAND"; CAND=""
    echo "already off in the file and in the running service (pid $rpid): no rewrite, no restart"
    validate "$PUB_ENV" false; return
  fi
  local backup=$STATE/publisher.env.before-rollback.$TS
  cp --preserve=mode,ownership,timestamps "$PUB_ENV" "$backup"; chown root:root "$backup"; chmod 0600 "$backup"
  validate "$CAND" false || { echo "rollback candidate invalid; live file left as is; backup $backup" >&2; return 1; }
  mv -f "$CAND" "$PUB_ENV"; CAND=""
  local since; since=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
  local pid; pid=$(restart_publisher) || { echo "$UNIT did not come back: journalctl -u $UNIT -n 80" >&2; return 1; }
  echo "$UNIT running again as pid $pid with auto_release=false (since $since); backup of the pre-rollback file: $backup"
  validate "$PUB_ENV" false
}

withdraw_hint() {
  echo "Anything AUTO_RELEASE released stays released. To withdraw one lecture (immediate, no restart):"
  echo "  sudo -u www-data $VENV_PY -m app.cli.publisher --env-file $PUB_ENV withdraw --lecture-id <uuid> --actor \"<name>\" --reason \"<why>\""
}

# ------------------------------------------------------------------- pre-flight (read-only, every mode)
say "0. code and service"
git merge-base --is-ancestor "$CODE_COMMIT" HEAD || die "HEAD $(git rev-parse --short HEAD) does not contain $CODE_COMMIT"
[ -z "$(git status --porcelain -- app backend)" ] || die "uncommitted changes under app/ or backend/: deploy a clean checkout"
! grep -q 'AUTO_RELEASE_FROM' app/publisher/config.py app/publisher/auto_release.py \
  || die "the deployed publisher still has the date restriction: pull the date-free automatic release first"
echo "HEAD $(git rev-parse --short HEAD) (contains $CODE_COMMIT; date-free automatic release; clean checkout)"
systemctl show -p EnvironmentFiles --value "$UNIT" | grep -q "$PUB_ENV" || die "$UNIT does not read $PUB_ENV"
systemctl is-active --quiet "$UNIT" || die "$UNIT is not active"
grep -qE '^PUBLISHER_MODE=PRODUCTION_WRITE$' "$PUB_ENV" || die "the publisher is not in PRODUCTION_WRITE"
grep -qE '^PUBLISHER_CDN_TOKEN_AUTH=DEFERRED$' "$PUB_ENV" || die "PUBLISHER_CDN_TOKEN_AUTH is not DEFERRED"
code_mtime=$(stat -c %Y app/publisher/delivery.py)
for u in "$UNIT" positive-moments.service; do
  upid=$(systemctl show -p MainPID --value "$u"); [ "${upid:-0}" != 0 ] || continue
  if [ "$(date -d "$(started_utc "$upid")" +%s)" -lt "$code_mtime" ]; then
    echo "NOTE: $u started before the current code was pulled: it still runs the previous code until it restarts"
    [ "$u" = positive-moments.service ] && echo "      (the Django API then still needs captions for READY: a caption-less lecture released" \
      && echo "       automatically would answer playback.available=false until positive-moments.service is restarted)"
  fi
done

if [ "$MODE" = rollback ]; then
  rollback_config || die "rollback incomplete (above)"
  withdraw_hint; exit 0
fi

if [ "$MODE" = verify ]; then
  say "verify (read-only)"
  validate "$PUB_ENV" true || die "live configuration does not have automatic release on"
  base=0; [ -f "$STATE/baseline_event_id" ] && base=$(cat "$STATE/baseline_event_id")
  reviewed=""; [ -f "$STATE/reviewed_ids" ] && reviewed=$(cat "$STATE/reviewed_ids")
  pid=$(main_pid); since=$(started_utc "$pid")
  FIRST_RUN_TIMEOUT=0 wait_runtime "$pid" "$since" "$base" "$reviewed" || die "verification failed or still pending (above); evidence $WORK"
  security || die "library security check failed"
  journal_errors "$since"
  say "VERIFIED: automatic release is on and healthy"; exit 0
fi

say "1. library security (read-only Bunny reads)"
security || die "library $LIB is not secure under the approved policy (CDN token auth DEFERRED)"

say "2. candidate configuration (validated before the live file is touched)"
CAND=$(dirname "$PUB_ENV")/.publisher.env.auto-release.$TS
write_candidate "$PUB_ENV" "$CAND" PUBLISHER_AUTO_RELEASE=true -PUBLISHER_AUTO_RELEASE_FROM
validate "$CAND" true || die "candidate configuration rejected by validate-config --check-library; live file unchanged"

say "3. PREVIEW: every lecture AUTO_RELEASE would release right now, all dates (read-only)"
probe candidates "$CAND" > "$WORK/preview.txt" || { cat "$WORK/preview.txt"; die "preview failed (above)"; }
sed '$d' "$WORK/preview.txt"
read -r ELIGIBLE_COUNT FINGERPRINT < <(python3 -c 'import json,sys; r=json.loads(open(sys.argv[1]).read().splitlines()[-1]); print(r["eligible_count"], r["fingerprint"])' "$WORK/preview.txt")
REVIEWED_IDS=$(python3 -c 'import json,sys; print(" ".join(json.loads(open(sys.argv[1]).read().splitlines()[-1])["eligible_ids"]))' "$WORK/preview.txt")
echo "eligible: $ELIGIBLE_COUNT   fingerprint: $FINGERPRINT   (full list, root-only: $WORK/preview.txt)"

say "4. the seven released lectures and the library state (read-only)"
probe snapshot "$PUB_ENV" | tee "$WORK/before.txt" || die "released state not as expected (above)"
BASE_EVENT=$(python3 -c 'import json,sys; print(json.loads(open(sys.argv[1]).readline())["max_event_id"])' "$WORK/before.txt")

if [ "$MODE" = preview ]; then
  say "PREVIEW DONE. Nothing changed."
  echo "Review the $ELIGIBLE_COUNT lecture(s) above. To activate with exactly this set:"
  echo "  sudo bash deploy/activate_auto_release.sh apply $ELIGIBLE_COUNT $FINGERPRINT"
  exit 0
fi
[ "$ELIGIBLE_COUNT" = "$REVIEWED_COUNT" ] && [ "$FINGERPRINT" = "$REVIEWED_PRINT" ]   || die "the eligible set changed since the reviewed preview ($REVIEWED_COUNT/$REVIEWED_PRINT -> $ELIGIBLE_COUNT/$FINGERPRINT): run preview again and review it"
echo "eligible set equals the reviewed preview: $ELIGIBLE_COUNT lecture(s), fingerprint $FINGERPRINT"

# ------------------------------------------------------------------- apply
say "5. live configuration"
if cmp -s "$CAND" "$PUB_ENV"; then
  echo "already PUBLISHER_AUTO_RELEASE=true: file not rewritten"
  CHANGED=0; rm -f "$CAND"; CAND=""
else
  BACKUP=$STATE/publisher.env.before-auto-release.$TS
  read -r OWNER GROUP PERM < <(stat -c '%U %G %a' "$PUB_ENV")
  cp --preserve=mode,ownership,timestamps "$PUB_ENV" "$BACKUP"; chown root:root "$BACKUP"; chmod 0600 "$BACKUP"
  cmp -s "$PUB_ENV" "$BACKUP" || die "backup differs from the live file"
  echo "backup: $BACKUP (root:root 0600)"
  mv -f "$CAND" "$PUB_ENV"; CAND=""
  CHANGED=1
  echo "applied atomically: $(stat -c '%U:%G %a' "$PUB_ENV") $(grep -cE '^PUBLISHER_AUTO_RELEASE(_FROM)?=' "$PUB_ENV") key line(s)"
fi
validate "$PUB_ENV" true || { [ "$CHANGED" = 1 ] && install -m "$PERM" -o "$OWNER" -g "$GROUP" "$BACKUP" "$PUB_ENV"; die "live file failed validation; restored, nothing restarted"; }
echo "$BASE_EVENT" > "$STATE/baseline_event_id"; echo "$REVIEWED_IDS" > "$STATE/reviewed_ids"

say "6. restart $UNIT ONLY"
MARKS_BEFORE=$(unit_marks)
pid=$(main_pid); since=$(started_utc "$pid")
if [ "$CHANGED" = 0 ] && [ "$(date -d "$since" +%s)" -ge "$(stat -c %Y "$PUB_ENV")" ] \
   && FIRST_RUN_TIMEOUT=0 wait_runtime "$pid" "$since" "$BASE_EVENT" "$REVIEWED_IDS" > /dev/null 2>&1; then
  echo "already running with automatic release on (pid $pid): no restart"
else
  since=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
  pid=$(restart_publisher) || { rollback_config || true; die "$UNIT did not come back healthy; configuration rolled back"; }
  echo "$UNIT active, new pid $pid (restarted $since)"
fi

say "7. scheduler, workers and the first AUTO_RELEASE run (up to $((FIRST_RUN_TIMEOUT / 60)) min)"
if ! wait_runtime "$pid" "$since" "$BASE_EVENT" "$REVIEWED_IDS"; then
  journal_errors "$since"
  rollback_config || true
  withdraw_hint
  die "activation not verified (above); configuration rolled back. Evidence: $WORK"
fi

say "8. after the first run"
# (step 7's probe already re-checked the seven, every new release event and every new import)
security || { rollback_config || true; die "library security check failed after the restart; configuration rolled back"; }
MARKS_AFTER=$(unit_marks)
[ "$MARKS_BEFORE" = "$MARKS_AFTER" ] && echo "positive-moments.service and the QA units: not restarted" \
  || echo "WARNING: another unit's start time changed during the run (not by this script)"
journal_errors "$since"
say "DONE: automatic release is ON (all dates; at most 25 per 5-minute run). Evidence (root-only): $WORK"
[ "$ELIGIBLE_COUNT" -le 25 ] || echo "The remaining reviewed lectures are released over the next runs: sudo bash deploy/activate_auto_release.sh verify"
echo "Turn it off: sudo bash deploy/activate_auto_release.sh rollback"
withdraw_hint
