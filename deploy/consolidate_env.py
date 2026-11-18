#!/usr/bin/env python3
"""
Consolidate the VPS runtime environment into ONE file: backend/.env.

Run on the VPS, as root, with the application's own interpreter, from the checkout:

    cd /var/www/Positive-moments
    sudo backend/venv/bin/python deploy/consolidate_env.py plan
    sudo backend/venv/bin/python deploy/consolidate_env.py apply
    sudo backend/venv/bin/python deploy/consolidate_env.py rollback --backup /root/kbc-env-consolidation/<stamp>

NOTHING IS EVER PRINTED BUT NAMES, PATHS, COUNTS AND VERDICTS. No value, URL, key or
token reaches stdout, stderr, the journal or an exception message.

WHAT IT PRESERVES - EXACTLY
---------------------------
For each unit (positive-moments, -media, -scheduler, -backfill, bunny-publisher) it
models the environment the service sees TODAY:

    Environment= lines  <  EnvironmentFile= files, in order (parsed BY SYSTEMD itself,
    via a throw-away `systemd-run ... env -0`), and, for the four QA units only,
    backend/.env filling names still missing (python-dotenv, override=False - what
    app/config/settings.py and Django settings do in-process).

It then builds ONE candidate backend/.env from every file-sourced name and models
what each unit would see AFTER (Environment= < backend/.env). It refuses unless,
for every name a unit actually reads (app/config/environment.py UNIT_VARIABLES),
the value is identical before and after - including "unset stays unset".

    * Two units reading one name with different values      -> CONFLICT, refused.
      (Resolve with --prefer NAME=UNIT only when you KNOW that value is right for every
      reader; the per-unit check then still lists what it changes.)
    * A name only a non-reader holds a different value for  -> resolved automatically
      to the readers' value (non-readers never look at it).
    * A unit would newly see a name it reads                -> refused unless
      --accept-change UNIT:NAME.
    * A critical name would be missing or empty             -> refused unless
      --accept-missing UNIT:NAME.

It then PROVES it, twice:
    1. round trip: the candidate parsed by systemd AND by python-dotenv must both give
       back exactly the planned values;
    2. application loading: for each unit, the real config classes (Settings,
       SchedulerConfig, the n8n gateway, Django settings, PublisherConfig and its
       problems()) are built in a child process from the BEFORE and the AFTER
       environment; every field is compared as a salted hash. Any difference refuses.
       No database or network connection is opened.

APPLY
-----
Refuses inside 20:30-01:00 Africa/Cairo (the 21:00 / 23:00 cycles) unless
--ignore-window. Then:
    1. backup to /root/kbc-env-consolidation/<stamp>/ (0700): every source env file,
       the current backend/.env, each unit's EnvironmentFiles list, a manifest;
    2. candidate written next to backend/.env (0640 root:www-data), fsynced,
       round-trip checked ON DISK, then rename(2)d over backend/.env - atomic;
    3. one drop-in per unit, /etc/systemd/system/<unit>.service.d/50-kbc-canonical-env.conf:
           [Service]
           EnvironmentFile=
           EnvironmentFile=/var/www/Positive-moments/backend/.env
       then daemon-reload, and each unit's EnvironmentFiles must now be exactly that file;
    4. the per-unit model is re-run against the LIVE systemd configuration.
Any failure in 2-4 rolls everything back automatically.

Running services are NOT restarted: they keep their current (identical) environment
until the operator restarts them with the printed command.

The old files (backend/config/.env, backend/qa-core.runtime.env,
backend/config/publisher.env) are never modified, moved or deleted.

ROLLBACK
--------
    sudo backend/venv/bin/python deploy/consolidate_env.py rollback --backup <dir>
removes the drop-ins, restores the previous backend/.env (atomically, same owner and
mode), daemon-reloads and checks every unit reads its original files again. Then
restart the units. The plain-shell equivalent is printed by apply.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.config import environment as registry  # noqa: E402  (names only; imports no app code)


DEFAULT_APP = Path("/var/www/Positive-moments")
SYSTEMD_DIR = Path("/etc/systemd/system")
BACKUP_ROOT = Path("/root/kbc-env-consolidation")
DROPIN_NAME = "50-kbc-canonical-env.conf"
MARKER = "# kbc-env-consolidation: written by deploy/consolidate_env.py"
SERVICE_USER, SERVICE_GROUP, FILE_MODE = "root", "www-data", 0o640
QUIET_START, QUIET_END = time(20, 30), time(1, 0)          # around the 21:00 / 23:00 Cairo cycles

UNITS = tuple(registry.UNIT_VARIABLES)
NAME_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
SAFE_VALUE = re.compile(r"[A-Za-z0-9_./:@%+,=?&~-]+")
ENV_FILE_ENTRY = re.compile(r"^(?P<path>.+?) \(ignore_errors=(?P<ignore>yes|no)\)$")

EXIT_OK, EXIT_REFUSED, EXIT_ROLLED_BACK, EXIT_ROLLBACK_INCOMPLETE = 0, 2, 3, 4


class Refused(Exception):
    """A reason to stop. Its message holds names and paths only, never a value."""


# --- host interfaces (replaced by fakes in tests) ----------------------------------------------

class Systemd:
    """systemctl / systemd-run. Never passes a value on a command line, never echoes output."""

    def __init__(self, run=subprocess.run):
        self._run = run

    def _out(self, *args: str) -> bytes:
        result = self._run(list(args), capture_output=True, check=False)
        if result.returncode != 0:
            raise Refused(f"`{' '.join(args[:2])}` failed (exit {result.returncode})")
        return result.stdout

    def show(self, unit: str, prop: str) -> str:
        return self._out("systemctl", "show", "-p", prop, "--value", f"{unit}.service").decode(
            "utf-8", "replace")

    def parse_env_file(self, path: Path) -> dict[str, str]:
        """The file exactly as systemd's EnvironmentFile= parser reads it."""
        out = self._out("systemd-run", "--quiet", "--pipe", "--wait", "--collect",
                        "-p", "User=nobody", "-p", f"EnvironmentFile={path}", "/usr/bin/env", "-0")
        seen: dict[str, str] = {}
        for item in out.split(b"\0"):
            key, sep, value = item.partition(b"=")
            if sep:
                seen[key.decode("utf-8", "replace")] = value.decode("utf-8", "surrogateescape")
        return {name: seen[name] for name in declared_names(path) if name in seen}

    def daemon_reload(self) -> None:
        self._out("systemctl", "daemon-reload")


class Host:
    """File ownership and identity. Separate so the plan logic can be tested anywhere."""

    def is_root(self) -> bool:
        return hasattr(os, "geteuid") and os.geteuid() == 0

    def chown(self, path: Path, user: str, group: str) -> None:
        shutil.chown(path, user=user, group=group)

    def chown_ids(self, path: Path, uid: int, gid: int) -> None:
        os.chown(path, uid, gid)

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


# --- parsing --------------------------------------------------------------------------------

def declared_names(path: Path) -> list[str]:
    names = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = NAME_LINE.match(line)
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return names


def dotenv_parse(path: Path) -> dict[str, str]:
    from dotenv import dotenv_values

    return {key: value for key, value in dotenv_values(path).items() if value is not None}


def render_value(name: str, value: str) -> str:
    """One line both systemd and python-dotenv read back identically (proven by the round trip)."""
    if value == "":
        return f"{name}="
    if SAFE_VALUE.fullmatch(value):
        return f"{name}={value}"            # unquoted: deploy scripts grep `^NAME=VALUE$`
    if any(ch in value for ch in ("'", "\\", "\n", "\r", "\0")):
        raise Refused(f"{name}: value contains a quote, backslash or line break; "
                      "edit it by hand into backend/.env and re-run")
    return f"{name}='{value}'"


def render(candidate: dict[str, str], stamp: str) -> str:
    lines = [
        "# backend/.env - the one runtime environment file for every backend unit.",
        f"# Consolidated by deploy/consolidate_env.py at {stamp}; see backend/.env.example.",
        "# Secrets: never commit, paste or log this file.",
        "",
    ]
    lines += [render_value(name, candidate[name]) for name in sorted(candidate)]
    return "\n".join(lines) + "\n"


# --- the model --------------------------------------------------------------------------------

@dataclass
class Unit:
    name: str
    loaded: bool
    user: str = ""
    env_files: list[tuple[Path, bool]] = field(default_factory=list)   # (path, ignore_errors)
    environment: dict[str, str] = field(default_factory=dict)          # Environment= lines
    exec_text: str = ""


def read_unit(sd: Systemd, name: str) -> Unit:
    if sd.show(name, "LoadState").strip() != "loaded":
        return Unit(name, loaded=False)
    files = []
    for line in sd.show(name, "EnvironmentFiles").splitlines():
        line = line.strip()
        if not line:
            continue
        match = ENV_FILE_ENTRY.match(line)
        if not match or not Path(match["path"]).is_absolute():
            raise Refused(f"{name}: unrecognised EnvironmentFiles entry")
        files.append((Path(match["path"]), match["ignore"] == "yes"))
    environment = {}
    for token in shlex.split(sd.show(name, "Environment").strip()):
        key, sep, value = token.partition("=")
        if sep:
            environment[key] = value
    return Unit(name, True, sd.show(name, "User").strip(), files, environment,
                sd.show(name, "ExecStartPre") + "\n" + sd.show(name, "ExecStart"))


@dataclass
class Plan:
    app: Path
    units: dict[str, Unit]
    before: dict[str, dict[str, str]]
    after: dict[str, dict[str, str]]
    candidate: dict[str, str]
    sources: dict[str, set[str]]                  # name -> source file paths
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def canonical(self) -> Path:
        return self.app / "backend" / ".env"


def build_plan(sd: Systemd, app: Path, prefer: dict[str, str], accept_change: set[str],
               accept_missing: set[str]) -> Plan:
    canonical = app / "backend" / ".env"
    units = {name: read_unit(sd, name) for name in UNITS}
    issues, notes = [], []
    for unit in units.values():
        if not unit.loaded:
            notes.append(f"{unit.name}: not installed on this host - skipped")
        elif unit.user not in ("www-data", "root", ""):
            issues.append(f"{unit.name}: runs as {unit.user!r}; backend/.env is root:www-data 0640")
        elif "--env-file" in unit.exec_text:
            issues.append(f"{unit.name}: ExecStart passes --env-file; review the unit by hand first")
    live = {name: unit for name, unit in units.items() if unit.loaded}

    parsed: dict[Path, dict[str, str]] = {}
    for unit in live.values():
        for path, ignore in unit.env_files:
            if path in parsed:
                continue
            if not path.is_file():
                if not ignore:
                    issues.append(f"{unit.name}: EnvironmentFile {path} is missing")
                continue
            parsed[path] = sd.parse_env_file(path)
    gap = dotenv_parse(canonical) if canonical.is_file() else {}

    before, file_sourced, provenance = {}, {}, {}
    for unit in live.values():
        effective, origin = dict(unit.environment), {}
        for path, _ in unit.env_files:
            for key, value in parsed.get(path, {}).items():
                effective[key], origin[key] = value, str(path)
        if unit.name in registry.QA_UNITS:              # in-process load_dotenv(override=False)
            for key, value in gap.items():
                if key not in effective:
                    effective[key], origin[key] = value, str(canonical)
        before[unit.name], file_sourced[unit.name], provenance[unit.name] = effective, origin, origin

    candidate, sources = {}, {}
    for key in sorted({k for origin in file_sourced.values() for k in origin}):
        holders = {u: before[u][key] for u in live if key in file_sourced[u]}
        sources[key] = {provenance[u][key] for u in holders}
        values = set(holders.values())
        if len(values) == 1:
            candidate[key] = values.pop()
            continue
        if key in prefer:
            chosen = prefer[key]
            if chosen not in holders:
                issues.append(f"--prefer {key}={chosen}: that unit does not hold {key}")
                continue
            candidate[key] = holders[chosen]
            notes.append(f"{key}: values differ; --prefer took {chosen}'s")
            continue
        readers = {u: v for u, v in holders.items() if key in registry.UNIT_VARIABLES[u]}
        if readers and len(set(readers.values())) == 1:
            candidate[key] = next(iter(readers.values()))
            notes.append(f"{key}: values differ only in units that never read it - kept the readers' value")
            continue
        issues.append(f"CONFLICT {key}: different values in {', '.join(sorted(sources[key]))} "
                      f"(read by {', '.join(sorted(readers)) or 'no unit'}); resolve with --prefer {key}=<unit>")

    after = {}
    for unit in live.values():
        effective = dict(unit.environment)
        effective.update(candidate)
        after[unit.name] = effective

    for name in live:
        reads = registry.UNIT_VARIABLES[name] | set(registry.CRITICAL_VARIABLES.get(name, ()))
        for key in sorted(reads):
            old, new = before[name].get(key), after[name].get(key)
            if old == new:
                continue
            kind = ("unset -> set" if old is None else "set -> unset" if new is None else "value differs")
            label = f"{name}:{key}"
            if label in accept_change:
                notes.append(f"{label}: {kind} (accepted)")
            else:
                issues.append(f"CHANGE {label}: {kind}; accept only after review with --accept-change {label}")
        for key in registry.CRITICAL_VARIABLES.get(name, ()):
            if not (after[name].get(key) or "").strip():
                label = f"{name}:{key}"
                if label in accept_missing:
                    notes.append(f"{label}: critical name empty (accepted)")
                else:
                    issues.append(f"MISSING {label}: critical name absent or empty "
                                  f"(--accept-missing {label} to proceed anyway)")
        unread = set(candidate) - set(before[name]) - registry.UNIT_VARIABLES[name]
        if unread:
            notes.append(f"{name}: {len(unread)} name(s) newly visible that it never reads")
    publisher = after.get(registry.PUBLISHER_UNIT)
    if publisher is not None and not (publisher.get("PUBLISHER_DATABASE_URL") or "").strip():
        issues.append("bunny-publisher: PUBLISHER_DATABASE_URL would be empty - it never falls back")
    try:
        render(candidate, "check")
    except Refused as exc:
        issues.append(str(exc))
    return Plan(app, units, before, after, candidate, sources, issues, notes)


# --- proofs --------------------------------------------------------------------------------

def check_round_trip(sd: Systemd, path: Path, expected: dict[str, str]) -> list[str]:
    problems = []
    for parser, values in (("python-dotenv", dotenv_parse(path)), ("systemd", sd.parse_env_file(path))):
        for key in sorted(set(expected) | set(values)):
            if values.get(key) != expected.get(key):
                problems.append(f"round trip ({parser}): {key} does not read back as planned")
    return problems


PROBE = r'''
import hashlib, json, os, sys
salt, kind, root = (os.environ.pop(k) for k in ("KBC_PROBE_SALT", "KBC_PROBE_KIND", "KBC_PROBE_ROOT"))
sys.path[:0] = [root, os.path.join(root, "backend")]
import app.config.environment as environment
# The environment passed in already holds every layer; the probe must not read any file.
environment.CANONICAL_ENV_FILE = environment.REPO_ROOT / "backend" / ".kbc-probe-no-such-file"
out = {}
def digest(value):
    return hashlib.sha256((salt + repr(value)).encode("utf-8", "surrogateescape")).hexdigest()
def grab(label, build):
    try:
        obj = build()
        fields = ({f: getattr(obj, f) for f in obj.__dataclass_fields__}
                  if hasattr(obj, "__dataclass_fields__") else obj)
        out[label] = {k: digest(v) for k, v in fields.items()}
    except Exception as exc:
        out[label] = {"__error__": digest(type(exc).__name__ + str(exc))}
if kind == "qa":
    from app.config.settings import Settings
    grab("settings", Settings.from_environment)
    from app.orchestration.scheduler import SchedulerConfig
    grab("scheduler", SchedulerConfig.from_environment)
    from app.orchestration.n8n_preflight import N8nReadOnlyGateway
    def n8n():
        gateway = N8nReadOnlyGateway.from_environment()
        return {"base_url": gateway.base_url, "api_key": gateway._api_key}
    grab("n8n", n8n)
    def django():
        import importlib
        module = importlib.import_module("config.settings")
        return {k: getattr(module, k) for k in dir(module) if k.isupper()}
    grab("django", django)
else:
    from app.publisher.config import PublisherConfig
    grab("publisher", PublisherConfig.from_environment)
    grab("publisher_problems", lambda: {"problems": PublisherConfig.from_environment().problems()})
print(json.dumps(out))
'''


def run_probe(app: Path, unit: str, env: dict[str, str], salt: str) -> dict:
    child = dict(env)
    if os.name == "nt":                                   # local tests only
        child.setdefault("SYSTEMROOT", os.environ.get("SYSTEMROOT", ""))
    child.update(KBC_PROBE_SALT=salt, KBC_PROBE_ROOT=str(app),
                 KBC_PROBE_KIND="publisher" if unit == registry.PUBLISHER_UNIT else "qa")
    child.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-I", "-c", PROBE], cwd=app, env=child,
                            capture_output=True, text=True, timeout=180, check=False)
    if result.returncode != 0:
        raise Refused(f"{unit}: application-loading probe failed (exit {result.returncode})")
    return json.loads(result.stdout.strip().splitlines()[-1])


def check_loading(plan: Plan, probe=run_probe) -> list[str]:
    problems, salt = [], secrets.token_hex(16)
    accepted_units = {n.split(":")[0] for n in plan.notes if "(accepted)" in n}
    for name in plan.before:
        old = probe(plan.app, name, plan.before[name], salt)
        new = probe(plan.app, name, plan.after[name], salt)
        for label in sorted(set(old) | set(new)):
            fields = sorted(f for f in set(old.get(label, {})) | set(new.get(label, {}))
                            if old.get(label, {}).get(f) != new.get(label, {}).get(f))
            if not fields:
                continue
            message = f"{name}: {label} differs after consolidation in field(s) {', '.join(fields)}"
            if name in accepted_units:
                plan.notes.append(message + " (unit has accepted changes)")
            else:
                problems.append("LOADING " + message)
    return problems


# --- report ---------------------------------------------------------------------------------

def report(plan: Plan, extra: list[str]) -> bool:
    print(f"canonical file: {plan.canonical}")
    for unit in plan.units.values():
        if unit.loaded:
            files = ", ".join(str(p) for p, _ in unit.env_files) or "(none)"
            print(f"  {unit.name}: EnvironmentFile {files}")
    shared = {k: v for k, v in plan.sources.items() if len(v) > 1}
    print(f"names in the consolidated file: {len(plan.candidate)}; held by more than one file: {len(shared)}")
    for key in sorted(shared):
        print(f"  {key}: {', '.join(sorted(shared[key]))}")
    for note in plan.notes:
        print(f"note: {note}")
    issues = plan.issues + extra
    for issue in issues:
        print(f"REFUSED: {issue}")
    print("VERDICT: " + ("READY" if not issues else f"REFUSED ({len(issues)} issue(s))"))
    return not issues


# --- apply / rollback --------------------------------------------------------------------------

def _write_atomic(path: Path, text: str, mode: int, owner: tuple[str, str] | None, host: Host) -> Path:
    tmp = path.with_name(f".{path.name}.kbc-tmp-{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    if owner and host.is_root():
        host.chown(tmp, *owner)
    return tmp


def _dropin(systemd_dir: Path, unit: str) -> Path:
    return systemd_dir / f"{unit}.service.d" / DROPIN_NAME


def _in_quiet_window(now: datetime) -> bool:
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo("Africa/Cairo")).time()
    return local >= QUIET_START or local < QUIET_END


def apply(plan: Plan, sd: Systemd, host: Host, systemd_dir: Path, backup_root: Path) -> int:
    stamp = host.now().strftime("%Y%m%dT%H%M%SZ")
    backup = backup_root / stamp
    backup.mkdir(parents=True, mode=0o700)
    files_dir = backup / "files"
    files_dir.mkdir(mode=0o700)
    live = [u for u in plan.units.values() if u.loaded]
    copies = {}
    for index, path in enumerate(sorted({p for u in live for p, _ in u.env_files} | {plan.canonical})):
        if path.is_file():
            target = files_dir / f"{index:02d}-{path.name.lstrip('.') or 'env'}"
            shutil.copy2(path, target)
            os.chmod(target, 0o600)
            st = path.stat()
            copies[str(path)] = {"copy": str(target), "mode": st.st_mode & 0o777,
                                 "uid": getattr(st, "st_uid", 0), "gid": getattr(st, "st_gid", 0)}
    dropins = {}
    for unit in live:
        existing = _dropin(systemd_dir, unit.name)
        if existing.is_file():
            target = files_dir / f"dropin-{unit.name}.conf"
            shutil.copy2(existing, target)
            dropins[unit.name] = str(target)
    manifest = {
        "stamp": stamp, "canonical": str(plan.canonical),
        "canonical_existed": plan.canonical.is_file(), "copies": copies,
        "previous_dropins": dropins, "units": [u.name for u in live],
        "env_files_before": {u.name: [[str(p), i] for p, i in u.env_files] for u in live},
    }
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    os.chmod(backup / "manifest.json", 0o600)
    print(f"backup: {backup}")

    try:
        tmp = _write_atomic(plan.canonical, render(plan.candidate, stamp), FILE_MODE,
                            (SERVICE_USER, SERVICE_GROUP), host)
        problems = check_round_trip(sd, tmp, plan.candidate)
        if problems:
            tmp.unlink()
            raise Refused("; ".join(problems))
        os.replace(tmp, plan.canonical)
        print(f"installed {plan.canonical} ({len(plan.candidate)} names, mode 0640)")
        for unit in live:
            path = _dropin(systemd_dir, unit.name)
            path.parent.mkdir(parents=True, exist_ok=True)
            text = (f"{MARKER} {stamp}\n# Remove this file and run `systemctl daemon-reload` to roll back.\n"
                    f"[Service]\nEnvironmentFile=\nEnvironmentFile={plan.canonical}\n")
            os.replace(_write_atomic(path, text, 0o644, None, host), path)
        sd.daemon_reload()
        for unit in live:
            got = [line.strip() for line in sd.show(unit.name, "EnvironmentFiles").splitlines() if line.strip()]
            if got != [f"{plan.canonical} (ignore_errors=no)"]:
                raise Refused(f"{unit.name}: after daemon-reload it does not read exactly {plan.canonical}")
        for unit in live:                                    # the live configuration, re-modelled
            effective = dict(read_unit(sd, unit.name).environment)
            effective.update(sd.parse_env_file(plan.canonical))
            if effective != plan.after[unit.name]:
                raise Refused(f"{unit.name}: live environment differs from the plan")
    except Exception as exc:
        reason = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        print(f"APPLY FAILED: {reason} - rolling back")
        code = rollback(backup, sd, host, systemd_dir)
        return EXIT_ROLLED_BACK if code == EXIT_OK else EXIT_ROLLBACK_INCOMPLETE
    units = " ".join(f"{u.name}.service" for u in live)
    print("APPLIED. Nothing was restarted; running services keep their current, identical environment.")
    print("Restart when ready (outside 20:30-01:00 Cairo, no backfill run active):")
    print(f"  systemctl restart --no-block {units}")
    print(f"  systemctl is-active {units}")
    print("Rollback:")
    print(f"  sudo {sys.executable} deploy/consolidate_env.py rollback --backup {backup}")
    print("Rollback without Python:")
    print(f"  sudo rm -f {' '.join(str(_dropin(systemd_dir, u.name)) for u in live)}")
    if manifest["canonical_existed"]:
        original = copies[str(plan.canonical)]
        print(f"  sudo install -m {original['mode']:o} -o {original['uid']} -g {original['gid']} "
              f"{original['copy']} {plan.canonical}")
    print(f"  sudo systemctl daemon-reload && sudo systemctl restart --no-block {units}")
    return EXIT_OK


def rollback(backup: Path, sd: Systemd, host: Host, systemd_dir: Path) -> int:
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    canonical = Path(manifest["canonical"])
    problems = []
    for unit in manifest["units"]:
        path = _dropin(systemd_dir, unit)
        previous = manifest["previous_dropins"].get(unit)
        if previous:
            shutil.copy2(previous, path)
        elif path.is_file() and path.read_text(encoding="utf-8").startswith(MARKER):
            path.unlink()                                   # only ever our own drop-in
    if manifest["canonical_existed"]:
        original = manifest["copies"][str(canonical)]
        tmp = canonical.with_name(f".{canonical.name}.kbc-restore-{secrets.token_hex(4)}")
        shutil.copy2(original["copy"], tmp)
        os.chmod(tmp, original["mode"])
        if host.is_root():
            host.chown_ids(tmp, original["uid"], original["gid"])
        os.replace(tmp, canonical)
    elif canonical.is_file():                               # we created it: keep it, out of the way
        os.replace(canonical, backup / "rolled-back-backend.env")
    sd.daemon_reload()
    for unit, files in manifest["env_files_before"].items():
        got = [line.strip() for line in sd.show(unit, "EnvironmentFiles").splitlines() if line.strip()]
        want = [f"{path} (ignore_errors={'yes' if ignore else 'no'})" for path, ignore in files]
        if got != want:
            problems.append(f"{unit}: EnvironmentFiles not back to the original list")
    for problem in problems:
        print(f"ROLLBACK INCOMPLETE: {problem}")
    if problems:
        return EXIT_ROLLBACK_INCOMPLETE
    units = " ".join(f"{u}.service" for u in manifest["units"])
    print(f"ROLLED BACK from {backup}. Restart to be sure every process uses the original files:")
    print(f"  systemctl restart --no-block {units}")
    return EXIT_OK


# --- CLI --------------------------------------------------------------------------------------

def _pairs(values: list[str], sep: str, flag: str) -> list[tuple[str, str]]:
    out = []
    for value in values:
        left, ok, right = value.partition(sep)
        if not ok or not left or not right:
            raise SystemExit(f"{flag} expects A{sep}B")
        out.append((left, right))
    return out


def main(argv=None, *, sd: Systemd | None = None, host: Host | None = None, probe=run_probe) -> int:
    parser = argparse.ArgumentParser(prog="consolidate_env.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("plan", "apply", "rollback"))
    parser.add_argument("--app", type=Path, default=DEFAULT_APP)
    parser.add_argument("--backup", type=Path, help="rollback: the backup directory apply printed")
    parser.add_argument("--prefer", action="append", default=[], metavar="NAME=UNIT")
    parser.add_argument("--accept-change", action="append", default=[], metavar="UNIT:NAME")
    parser.add_argument("--accept-missing", action="append", default=[], metavar="UNIT:NAME")
    parser.add_argument("--ignore-window", action="store_true")
    parser.add_argument("--systemd-dir", type=Path, default=SYSTEMD_DIR, help=argparse.SUPPRESS)
    parser.add_argument("--backup-root", type=Path, default=BACKUP_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    sd, host = sd or Systemd(), host or Host()

    if not host.is_root():
        print("REFUSED: run as root (sudo) - systemd parsing, backups and drop-ins need it")
        return EXIT_REFUSED
    try:
        if args.command == "rollback":
            if not args.backup:
                raise SystemExit("rollback needs --backup <dir>")
            return rollback(args.backup, sd, host, args.systemd_dir)
        plan = build_plan(sd, args.app, dict(_pairs(args.prefer, "=", "--prefer")),
                          set(args.accept_change), set(args.accept_missing))
        extra = []
        if not plan.issues:
            with_tmp = args.backup_root / f".plan-{secrets.token_hex(4)}"
            with_tmp.mkdir(parents=True, mode=0o700)
            try:
                candidate_file = with_tmp / "candidate.env"
                candidate_file.write_text(render(plan.candidate, "plan"), encoding="utf-8")
                os.chmod(candidate_file, 0o600)
                extra += check_round_trip(sd, candidate_file, plan.candidate)
            finally:
                shutil.rmtree(with_tmp, ignore_errors=True)
            if not extra:
                extra += check_loading(plan, probe)
        if args.command == "apply" and not args.ignore_window and _in_quiet_window(host.now()):
            extra.append("inside the 20:30-01:00 Africa/Cairo scheduler window (--ignore-window to override)")
        ready = report(plan, extra)
        if not ready:
            return EXIT_REFUSED
        if args.command == "plan":
            print("plan only: nothing was written. Run `apply` to make the change.")
            return EXIT_OK
        return apply(plan, sd, host, args.systemd_dir, args.backup_root)
    except Refused as exc:
        print(f"REFUSED: {exc}")
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
