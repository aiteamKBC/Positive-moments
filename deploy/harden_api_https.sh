#!/usr/bin/env bash
# =============================================================================================
# HTTPS hardening of the Positive Moments OpenLiteSpeed virtual host (task A, 24 Oct 2026).
#
#   sudo bash deploy/harden_api_https.sh inspect    # read-only: which vhost/config/listeners, current answers
#   sudo bash deploy/harden_api_https.sh apply      # backup, add the rules, graceful OLS restart, verify
#   sudo bash deploy/harden_api_https.sh verify     # read-only checks (re-runnable)
#   sudo bash deploy/harden_api_https.sh rollback   # restore the newest backup, graceful OLS restart
#
# What it adds (rules: deploy/kbc_api_https_rules.py): ONE marked block at the top of the vhost-level rewrite rules of the vhost that
# serves positive-moments.kentbusinesscollege.net, and nothing else:
#   * plain-HTTP requests (listener port 80) for /api/publisher/ are answered 403 by OpenLiteSpeed
#     itself: they never reach Django. (A redirect would not help: a Bearer token sent over HTTP is
#     already exposed, and following the redirect would re-send it.) The LMS must use https://.
#   * the staff API documentation (/api/docs..., /api/schema...): plain-HTTP GET/HEAD are redirected
#     301 to https://, any other method is answered 403 by OpenLiteSpeed: the sign-in form is never
#     served, and a password never processed, over HTTP (2026-11-04).
#   * plain-HTTP GET/HEAD of normal pages are redirected 301 to https:// on the fixed hostname.
#   * unchanged: every other /api/ route over HTTP (unknown external callers, POST bodies), and
#     /.well-known/acme-challenge/ (Let's Encrypt HTTP-01 renewal). HTTPS is not changed at all.
# Safety: refuses a vhost defined by a shared vhTemplate, refuses to switch on a disabled rewrite
# block that has rules of its own, is idempotent (the marked block is replaced, never duplicated),
# keeps a root-only backup, and restores it automatically if any post-change check fails.
# =============================================================================================
set -Eeuo pipefail
umask 077
readonly DOMAIN=positive-moments.kentbusinesscollege.net
readonly LSWS=/usr/local/lsws
readonly CTRL=$LSWS/bin/lswsctrl
readonly TS=$(date -u +%Y%m%dT%H%M%SZ)
readonly BACKUPS=/root/kbc-https
readonly HERE=$(cd "$(dirname "$0")" && pwd)
MODE=${1:-}
die() { printf '\nABORT: %s\n' "$*" >&2; exit 1; }
say() { printf '\n== %s\n' "$*"; }
case "$MODE" in inspect|apply|verify|rollback) ;; *) echo "usage: sudo bash $0 inspect|apply|verify|rollback"; exit 64;; esac
[ "$(id -u)" -eq 0 ] || die "run as root"
[ -x "$CTRL" ] && [ -f "$LSWS/conf/httpd_config.conf" ] || die "OpenLiteSpeed not found under $LSWS"
mkdir -p -m 0700 "$BACKUPS"

# ---------------------------------------------------------------- locate the vhost config file
locate() {
  python3 - "$LSWS" "$DOMAIN" <<'PY'
import re, sys
root, dom = sys.argv[1], sys.argv[2]
conf = open(f"{root}/conf/httpd_config.conf", encoding="utf-8", errors="replace").read()
def blocks(kind):
    out = {}
    for m in re.finditer(rf"^\s*{kind}\s+(\S+)\s*\{{", conf, re.M):
        depth, i = 1, m.end()
        while depth and i < len(conf):
            depth += {"{": 1, "}": -1}.get(conf[i], 0); i += 1
        out[m.group(1)] = conf[m.end():i - 1]
    return out
listeners, vhosts, templates = blocks("listener"), blocks("virtualhost"), blocks("vhTemplate")
exact, http_ports = set(), set()
for lname, body in listeners.items():
    secure = re.search(r"^\s*secure\s+1\b", body, re.M) is not None
    port = (re.search(r"^\s*address\s+\S*:(\d+)", body, re.M) or [None, "?"])[1]
    for m in re.finditer(r"^\s*map\s+(\S+)\s+(.+)$", body, re.M):
        doms = [d.strip() for d in m.group(2).split(",")]
        if dom in doms:
            exact.add(m.group(1))
            if not secure: http_ports.add(port)
            print(f"# listener {lname}: port {port} secure={int(secure)} -> vhost {m.group(1)}", file=sys.stderr)
for tname, body in templates.items():
    for name in exact:
        if re.search(rf"^\s*member\s+{re.escape(name)}\b", body, re.M):
            sys.exit(f"vhost {name} is a member of the shared template {tname}: edit by hand")
if len(exact) != 1: sys.exit(f"expected exactly one vhost mapped to {dom}, found {sorted(exact)}")
name = exact.pop()
if name not in vhosts: sys.exit(f"vhost {name} has no virtualhost block in httpd_config.conf")
body = vhosts[name]
vh_root = (re.search(r"^\s*vhRoot\s+(\S+)", body, re.M) or [None, ""])[1]
cfg = (re.search(r"^\s*configFile\s+(\S+)", body, re.M) or [None, ""])[1]
sub = lambda s: s.replace("$SERVER_ROOT", root).replace("$VH_NAME", name).replace("$VH_ROOT", vh_root.replace("$SERVER_ROOT", root).replace("$VH_NAME", name))
if not cfg: sys.exit(f"vhost {name} has no configFile")
if http_ports != {"80"}: sys.exit(f"plain-HTTP listener ports for {dom}: {sorted(http_ports)} (expected exactly 80)")
print(sub(cfg))
PY
}

# ---------------------------------------------------------------- the checks (curl, status codes only)
probe() {   # $1 url [$2 method] -> "status contract_header redirect"
  local h; h=$(mktemp); local body=()
  [ "${2:-GET}" = POST ] && body=(--data-binary "")          # an empty body: Content-Length 0, no credentials
  local code; code=$(curl -s -o /dev/null -D "$h" -w '%{http_code}' --max-time 20 -X "${2:-GET}" "${body[@]}" "$1" || true)
  printf '%s %s %s\n' "$code" "$(grep -ci '^x-publisher-contract:' "$h" || true)" \
    "$(grep -i '^location:' "$h" | tr -d '\r' | awk '{print $2}' | head -1)"
  rm -f "$h"
}
answers() {
  for u in "http://$DOMAIN/api/publisher/v1/health" "http://$DOMAIN/api/publisher/v1/occurrences/OCC-DOES-NOT-EXIST/recording" \
           "https://$DOMAIN/api/publisher/v1/health" "http://$DOMAIN/" "http://$DOMAIN/login?next=x" "https://$DOMAIN/" \
           "http://$DOMAIN/api/health/" "https://$DOMAIN/api/health/" "http://$DOMAIN/.well-known/acme-challenge/kbc-probe"; do
    printf '%-80s %s\n' "$u" "$(probe "$u")"
  done
  for u in "http://$DOMAIN/api/docs/" "http://$DOMAIN/api/docs/login/?next=/api/docs/" "http://$DOMAIN/api/schema/" \
           "https://$DOMAIN/api/docs/" "https://$DOMAIN/api/schema/"; do
    printf '%-80s %s\n' "$u" "$(probe "$u")"
  done
  for u in "http://$DOMAIN/api/docs/login/" "http://$DOMAIN/api/docs/logout/"; do   # no body, no credentials
    printf '%-80s %s\n' "POST:$u" "$(probe "$u" POST)"
  done
}
check() {   # exit 1 on any failure; prints a table
  python3 - "$1" "$2" "$DOMAIN" <<'PY'
import sys
def load(p):
    return {l.split()[0]: l.split()[1:] for l in open(p) if l.strip()}
before, now, dom = load(sys.argv[1]), load(sys.argv[2]), sys.argv[3]
def a(u): return now.get(u, ["?", "0", ""]) + [""] * 3
fails = []
def need(ok, what):
    print(("PASS " if ok else "FAIL ") + what); ok or fails.append(what)
for u in (f"http://{dom}/api/publisher/v1/health", f"http://{dom}/api/publisher/v1/occurrences/OCC-DOES-NOT-EXIST/recording"):
    s, c, _ = a(u)[:3]
    need(s == "403" and c == "0", f"HTTP API refused by the web server (403, Django not reached): {u} -> {s}, contract header {c}")
s, c, _ = a(f"https://{dom}/api/publisher/v1/health")[:3]
need(s == "401" and c == "1", f"HTTPS API still served by Django and still demands a token -> {s}, contract header {c}")
s, _, loc = a(f"http://{dom}/")[:3]
need(s == "301" and loc == f"https://{dom}/", f"HTTP page redirected to HTTPS -> {s} {loc}")
s, _, loc = a(f"http://{dom}/login?next=x")[:3]
need(s == "301" and loc == f"https://{dom}/login?next=x", f"HTTP page redirect keeps path and query -> {s} {loc}")
need(a(f"https://{dom}/")[0] == before.get(f"https://{dom}/", ["?"])[0], "HTTPS website answer unchanged")
for u in (f"http://{dom}/api/health/", f"https://{dom}/api/health/"):
    need(a(u)[0] == before.get(u, ["?"])[0], f"unrelated API route unchanged: {u} -> {a(u)[0]}")
for path in ("/api/docs/", "/api/docs/login/?next=/api/docs/", "/api/schema/"):
    s, _, loc = a(f"http://{dom}{path}")[:3]
    need(s == "301" and loc == f"https://{dom}{path}", f"HTTP docs page redirected to HTTPS, never served -> {path}: {s} {loc}")
for path in ("/api/docs/login/", "/api/docs/logout/"):
    s = a(f"POST:http://{dom}{path}")[0]
    need(s == "403", f"HTTP docs POST refused by the web server (no credentials processed) -> {path}: {s}")
for path in ("/api/docs/", "/api/schema/"):
    s = a(f"https://{dom}{path}")[0]
    need(s in ("302", "404"), f"HTTPS docs still served by Django (302 sign-in when enabled, 404 when off) -> {path}: {s}")
s = a(f"http://{dom}/.well-known/acme-challenge/kbc-probe")[0]
need(s != "301" and s != "403", f"ACME HTTP-01 path not redirected or refused -> {s}")
sys.exit(1 if fails else 0)
PY
}
graceful() { "$CTRL" restart >/dev/null; sleep 4; "$CTRL" status | head -2 || true; }

if [ "$MODE" = rollback ]; then
  last=$(ls -1d "$BACKUPS"/*/ 2>/dev/null | tail -1) || true; [ -n "$last" ] || die "no backup in $BACKUPS"
  target=$(cat "$last/target"); cp --preserve=all "$last/vhconf.conf" "$target"
  say "restored $target from $last"; graceful; answers; exit 0
fi

say "locate the vhost of $DOMAIN"
VHCONF=$(locate) || die "could not locate the vhost config (see above)"
[ -f "$VHCONF" ] || die "config file $VHCONF does not exist"
stat -c '%n  %U:%G %a' "$VHCONF"
grep -nE '^\s*(docRoot|vhDomain|context|extprocessor|address|type|handler|location|uri)\b|^\s*rewrite\s*\{|^\s*(enable|autoLoadHtaccess)\b|RewriteRule|RewriteCond' "$VHCONF" \
  | sed -E 's/(password|secret|key|token)[^ ]*.*/\1 <hidden>/I' | head -60
grep -rn "8029" "$LSWS/conf" 2>/dev/null | grep -v '\.bak' | head -5 || true
WORK=$BACKUPS/$TS; mkdir -p -m 0700 "$WORK"
answers > "$WORK/before.txt"; cat "$WORK/before.txt"
if [ "$MODE" = inspect ]; then say "INSPECT done: nothing changed"; exit 0; fi
if [ "$MODE" = verify ]; then
  first=$(ls -1 "$BACKUPS"/*/before.txt 2>/dev/null | head -1)
  check "${first:-$WORK/before.txt}" "$WORK/before.txt" && say "VERIFIED" || die "verification failed"; exit 0
fi

say "backup"
cp --preserve=all "$VHCONF" "$WORK/vhconf.conf"; printf '%s\n' "$VHCONF" > "$WORK/target"
cmp -s "$VHCONF" "$WORK/vhconf.conf" || die "backup differs"; echo "backup: $WORK/vhconf.conf"

say "add the marked rewrite block (vhost level, first rules)"
python3 "$HERE/kbc_api_https_rules.py" "$VHCONF" "$DOMAIN"
diff -u "$WORK/vhconf.conf" "$VHCONF" | sed -E 's/(password|secret|key|token)[^ ]*.*/\1 <hidden>/I' || true

say "graceful OpenLiteSpeed restart"
graceful
answers > "$WORK/after.txt"; cat "$WORK/after.txt"
if ! check "$WORK/before.txt" "$WORK/after.txt"; then
  cp --preserve=all "$WORK/vhconf.conf" "$VHCONF"; graceful
  die "a check failed: the previous configuration was restored and OpenLiteSpeed restarted (evidence: $WORK)"
fi
say "DONE. Backup: $WORK/vhconf.conf   Rollback: sudo bash deploy/harden_api_https.sh rollback"
