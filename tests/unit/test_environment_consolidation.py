"""
backend/.env is the one runtime env file (app/config/environment.py), and the VPS migration
(deploy/consolidate_env.py) preserves every service's effective configuration.

No test here reads a real env file, opens a database or touches systemd: the migration runs
against a fake systemd over temporary files, and every assertion about output checks that no
value ever reaches it.
"""
from __future__ import annotations

import importlib.util
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import environment
from tools import integration_db_guard as guard

REPO = Path(__file__).resolve().parents[2]

spec = importlib.util.spec_from_file_location("consolidate_env", REPO / "deploy" / "consolidate_env.py")
consolidate = importlib.util.module_from_spec(spec)
sys.modules["consolidate_env"] = consolidate
spec.loader.exec_module(consolidate)


# --- the registry stays complete -----------------------------------------------------------------

READ_CALL = re.compile(
    r"""(?:getenv|environ\.get|environ\[|env_bool|_ratio|_non_negative|\btext|\bnumber|\bflag|\blibrary)"""
    r"""\(\s*["']([A-Z][A-Z0-9_]+)["']""")
READERS = ("app/config/settings.py", "app/orchestration/n8n_preflight.py", "app/publisher/config.py",
           "backend/config/settings.py")


def test_every_name_the_code_reads_is_in_the_registry():
    from app.cli import bunny
    from app.orchestration.scheduler import ENVIRONMENT_VARIABLES

    read = set()
    for path in READERS:
        read |= set(READ_CALL.findall((REPO / path).read_text(encoding="utf-8")))
    read |= set(ENVIRONMENT_VARIABLES.values()) | set(bunny.NAMES)
    known = set().union(*environment.COMPONENT_VARIABLES.values())
    assert read - known == set()
    # the publisher no longer reads DATABASE_URL at all
    assert "DATABASE_URL" not in READ_CALL.findall((REPO / "app/publisher/config.py").read_text(encoding="utf-8"))
    assert "DATABASE_URL" not in environment.PUBLISHER_VARIABLES


def test_the_example_lists_every_registered_name_and_holds_no_secret():
    text = (REPO / "backend" / ".env.example").read_text(encoding="utf-8")
    listed = dict(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=(.*)$", text, re.MULTILINE))
    known = set().union(*environment.COMPONENT_VARIABLES.values())
    assert known - set(listed) == set()
    for name, value in listed.items():
        if re.search(r"KEY|SECRET|TOKEN|PASSWORD|DATABASE_URL", name) and name != "DJANGO_SECRET_KEY":
            assert value.strip() == "", name
    assert listed["PUBLISHER_MODE"] == "SHADOW" and listed["PUBLISHER_PRODUCTION_WRITE_ENABLED"] == "false"
    assert not any(name.startswith("VITE_") for name in listed)        # browser config stays in frontend/


# --- every loader: process environment first, backend/.env fills gaps --------------------------

@pytest.fixture
def canonical(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(environment, "CANONICAL_ENV_FILE", path)
    return path


def test_settings_fill_gaps_from_backend_env_and_never_override(canonical, monkeypatch):
    from app.config.settings import Settings

    canonical.write_text("QA_MODEL_NAME=from-file\nRECORDING_LINK_SEARCH_REGION=eur\n", encoding="utf-8")
    monkeypatch.setenv("QA_MODEL_NAME", "from-process")
    monkeypatch.delenv("RECORDING_LINK_SEARCH_REGION", raising=False)
    settings = Settings.from_environment()
    assert settings.qa_model_name == "from-process"
    assert settings.recording_link_search_region == "EUR"


def test_scheduler_config_fills_gaps_from_backend_env(canonical, monkeypatch):
    from app.orchestration.scheduler import SchedulerConfig

    canonical.write_text("SCHEDULER_LOOKBACK_DAYS=9\nSCHEDULER_CRON=0 1 * * *\n", encoding="utf-8")
    monkeypatch.delenv("SCHEDULER_LOOKBACK_DAYS", raising=False)
    monkeypatch.setenv("SCHEDULER_CRON", "0 21,23 * * *")
    config = SchedulerConfig.from_environment()
    assert (config.lookback_days, config.cron) == (9, "0 21,23 * * *")


def test_n8n_gateway_reads_only_backend_env_after_the_process(canonical, monkeypatch, tmp_path):
    from app.orchestration.n8n_preflight import N8nReadOnlyGateway

    canonical.write_text("N8N_BASE_URL=https://n8n.example.invalid\nN8N_API_KEY=file-key\n", encoding="utf-8")
    monkeypatch.delenv("N8N_BASE_URL", raising=False)
    monkeypatch.setenv("N8N_API_KEY", "process-key")
    gateway = N8nReadOnlyGateway.from_environment()
    assert gateway.base_url == "https://n8n.example.invalid" and gateway._api_key == "process-key"
    source = (REPO / "app/orchestration/n8n_preflight.py").read_text(encoding="utf-8")
    assert '".env")' not in source                                    # the root .env fallback is gone


def test_bunny_cli_reads_backend_env_through_the_shared_helper(canonical, monkeypatch):
    from app.cli import bunny

    canonical.write_text("BUNNY_STAGING_LIBRARY_ID=123\n", encoding="utf-8")
    monkeypatch.delenv("BUNNY_STAGING_LIBRARY_ID", raising=False)
    assert bunny._settings()["BUNNY_STAGING_LIBRARY_ID"] == "123"


def test_django_settings_load_the_same_file_through_the_same_helper():
    source = (REPO / "backend/config/settings.py").read_text(encoding="utf-8")
    assert "load_canonical_env()" in source and "load_dotenv" not in source
    assert environment.CANONICAL_ENV_FILE == REPO / "backend" / ".env"


def test_no_runtime_file_or_unit_references_the_old_env_files():
    old = ("backend/config/.env", "qa-core.runtime.env", "backend/config/publisher.env")
    checked = ["deploy/systemd/positive-moments-media.service", "deploy/systemd/bunny-publisher.service",
               "deploy/activate_phase_c.sh", "deploy/activate_phase_d.sh", "deploy/activate_auto_release.sh",
               "app/config/settings.py", "backend/config/settings.py", "app/publisher/config.py",
               "app/orchestration/n8n_preflight.py", "app/cli/bunny.py", "app/orchestration/scheduler.py",
               "tools/publisher_replay_branch.py"]
    for path in checked:
        text = (REPO / path).read_text(encoding="utf-8")
        assert not any(name in text for name in old), path
    for unit in ("positive-moments-media", "bunny-publisher"):
        text = (REPO / "deploy" / "systemd" / f"{unit}.service").read_text(encoding="utf-8")
        directives = [l for l in text.splitlines() if l.startswith("EnvironmentFile=")]
        assert directives == ["EnvironmentFile=/var/www/Positive-moments/backend/.env"], unit


# --- the integration-test database guard ---------------------------------------------------------

def test_the_guard_treats_legacy_files_and_every_database_url_name_as_production(tmp_path, monkeypatch):
    for legacy in environment.LEGACY_ENV_FILES:
        assert legacy in guard.PRODUCTION_ENV_FILES
    assert environment.CANONICAL_ENV_FILE in guard.PRODUCTION_ENV_FILES
    assert guard.is_production_url_key("PUBLISHER_DATABASE_URL")
    assert guard.is_production_url_key("COMMUNICATION_CENTRE_DATABASE_URL")
    assert not guard.is_production_url_key(guard.TEST_URL_ENV)
    env_file = tmp_path / "publisher.env"
    env_file.write_text("PUBLISHER_DATABASE_URL=postgresql://u:p@prod.example.invalid:5432/live\n",
                        encoding="utf-8")
    monkeypatch.setattr(guard, "PRODUCTION_ENV_FILES", (env_file,))
    urls = guard.production_urls({"LMS_DATABASE_URL": "postgresql://u:p@lms.example.invalid/lms",
                                  guard.TEST_URL_ENV: "postgresql://u:p@127.0.0.1:5432/kbc_qa_integration_test"})
    assert len(urls) == 2 and not any("kbc_qa_integration_test" in url for url in urls)
    with pytest.raises(guard.UnsafeTestDatabase):
        guard.check_static({guard.TEST_URL_ENV: "postgresql://u:p@prod.example.invalid:5432/kbc_qa_integration_test",
                            guard.APP_ENV: "test", guard.ALLOW_REMOTE_ENV: "1"})


# --- the VPS migration -------------------------------------------------------------------------

SECRET = "S3CRET-" + secrets.token_hex(6)


class FakeHost:
    def __init__(self, hour_utc=8):
        self._now = datetime(2026, 11, 18, hour_utc, 0, tzinfo=timezone.utc)

    def is_root(self):
        return True

    def chown(self, *_):
        pass

    def chown_ids(self, *_):
        pass

    def now(self):
        return self._now


class FakeSystemd:
    """systemctl show / systemd-run / daemon-reload over plain files; drop-ins are honoured."""

    def __init__(self, units, systemd_dir, canonical):
        self.units, self.systemd_dir, self.canonical = units, systemd_dir, canonical
        self.original = {name: (list(unit["files"]), unit.get("environment", "")) for name, unit in units.items()}
        self.tamper = None
        self.running = {}                   # unit -> set of NAMES in the running process

    def show(self, unit, prop):
        u = self.units.get(unit)
        if prop == "LoadState":
            return "loaded" if u else "not-found"
        if prop == "EnvironmentFiles":
            return "\n".join(f"{p} (ignore_errors={'yes' if i else 'no'})" for p, i in u["files"])
        return {"Environment": u.get("environment", ""), "User": "www-data",
                "ExecStart": u.get("exec", "python -m app.cli.x run"), "ExecStartPre": "",
                "UnsetEnvironment": u.get("unset", ""), "MainPID": "0"}[prop]

    def running_names(self, unit):
        return self.running.get(unit)

    def parse_env_file(self, path):
        values = consolidate.dotenv_parse(Path(path))
        return self.tamper(Path(path), values) if self.tamper else values

    def daemon_reload(self):
        for name, unit in self.units.items():
            files, environment_line = self.original[name]
            dropin = self.systemd_dir / f"{name}.service.d" / consolidate.DROPIN_NAME
            text = dropin.read_text(encoding="utf-8") if dropin.is_file() else ""
            if "EnvironmentFile=\n" in text:
                lines = text.splitlines()
                unit["files"] = [(Path(lines[lines.index("EnvironmentFile=") + 1].split("=", 1)[1]), False)]
                extra = [l.split("=", 1)[1] for l in lines if l.startswith("Environment=")]
                unit["environment"] = " ".join(filter(None, [environment_line, *extra]))
                unit["unset"] = " ".join(l.split("=", 1)[1] for l in lines if l.startswith("UnsetEnvironment="))
            else:
                unit["files"], unit["environment"], unit["unset"] = list(files), environment_line, ""


SHARED = {"DATABASE_URL": f"postgresql://u:{SECRET}@pool.example.invalid/qa",
          "MICROSOFT_GRAPH_TENANT_ID": "tenant", "MICROSOFT_GRAPH_CLIENT_ID": "client",
          "MICROSOFT_GRAPH_CLIENT_SECRET": SECRET, "APTEM_DATABASE_URL": f"postgresql://a:{SECRET}@aptem/x"}


def _write(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return path


@pytest.fixture
def vps(tmp_path):
    app = tmp_path / "app"
    # Today's QA files: both hold every name the QA units read, with the same values.
    qa = {**SHARED, "DJANGO_SECRET_KEY": SECRET, "QA_MODEL_API_KEY": SECRET, "PUBLISHER_LMS_LIBRARY_ID": "786045",
          "SCHEDULER_ENABLED": "true", "SCHEDULER_CRON": "0 21,23 * * *"}
    web = _write(app / "backend" / "config" / ".env", {**qa, "MICROSOFT_CLIENT_ID_2": "other"})
    runtime = _write(app / "backend" / "qa-core.runtime.env", qa)
    publisher = _write(app / "backend" / "config" / "publisher.env", {
        **{k: v for k, v in SHARED.items() if k != "DATABASE_URL"},
        "PUBLISHER_DATABASE_URL": f"postgresql://p:{SECRET}@direct.example.invalid/li",
        "PUBLISHER_MODE": "PRODUCTION_WRITE", "PUBLISHER_PRODUCTION_WRITE_ENABLED": "true",
        "PUBLISHER_START_DATE": "2026-10-23", "LMS_DATABASE_URL": f"postgresql://l:{SECRET}@lms/x",
        "BUNNY_PRODUCTION_API_KEY": SECRET})
    canonical = _write(app / "backend" / ".env", {"N8N_BASE_URL": "https://n8n.example.invalid",
                                                  "N8N_API_KEY": SECRET})
    # The QA units get LMS_DATABASE_URL too (same value) so the merge changes nothing they read.
    with web.open("a", encoding="utf-8") as fh:
        fh.write(f"LMS_DATABASE_URL=postgresql://l:{SECRET}@lms/x\n")
    with runtime.open("a", encoding="utf-8") as fh:
        fh.write(f"LMS_DATABASE_URL=postgresql://l:{SECRET}@lms/x\n")
    units = {
        "positive-moments": {"files": [(web, False)], "environment": "PYTHONPATH=/srv PATH=/srv/bin"},
        "positive-moments-media": {"files": [(web, False)], "environment": "PYTHONPATH=/srv"},
        "positive-moments-scheduler": {"files": [(runtime, False)]},
        "positive-moments-backfill": {"files": [(runtime, False)]},
        "bunny-publisher": {"files": [(publisher, False)], "environment": "TZ=Africa/Cairo"},
    }
    systemd_dir = tmp_path / "systemd"
    sd = FakeSystemd(units, systemd_dir, canonical)
    return {"app": app, "sd": sd, "systemd_dir": systemd_dir, "backups": tmp_path / "backups",
            "canonical": canonical, "old": (web, runtime, publisher)}


def identical_probe(app, unit, env, salt):
    """
    Stands in for the child-process probe: every name the unit reads, under each label the unit
    compares (as the always-compared __error__ key where the label filters fields).
    """
    reads = sorted(environment.UNIT_VARIABLES[unit])
    out = {}
    for label, fields in environment.UNIT_PROBES[unit].items():
        out[label] = ({k: str(hash((salt, env.get(k)))) for k in reads} if fields is None
                      else {"__error__": str(hash((salt, tuple(env.get(k) for k in reads))))})
    return out


def real_probe(app, unit, env, salt):
    """The real child-process probe, run against this checkout's code."""
    return consolidate.run_probe(REPO, unit, env, salt)


def run(vps, *args, host=None, probe=identical_probe):
    argv = [*args, "--app", str(vps["app"]), "--systemd-dir", str(vps["systemd_dir"]),
            "--backup-root", str(vps["backups"])]
    return consolidate.main(argv, sd=vps["sd"], host=host or FakeHost(), probe=probe)


def test_plan_is_ready_and_writes_nothing(vps, capsys):
    before = {p: p.read_bytes() for p in (*vps["old"], vps["canonical"])}
    assert run(vps, "plan") == consolidate.EXIT_OK
    out = capsys.readouterr().out
    assert "VERDICT: READY" in out and SECRET not in out and "postgresql://" not in out
    assert {p: p.read_bytes() for p in before} == before
    assert not vps["systemd_dir"].exists()


def test_apply_installs_one_file_points_every_unit_at_it_and_preserves_every_value(vps, capsys):
    old_contents = {p: p.read_bytes() for p in vps["old"]}
    assert run(vps, "apply") == consolidate.EXIT_OK
    out = capsys.readouterr().out
    assert "APPLIED" in out and SECRET not in out and "postgresql://" not in out
    merged = consolidate.dotenv_parse(vps["canonical"])
    assert merged["PUBLISHER_MODE"] == "PRODUCTION_WRITE" and merged["N8N_API_KEY"] == SECRET
    assert merged["PUBLISHER_DATABASE_URL"] != merged["DATABASE_URL"]
    assert "PUBLISHER_MODE=PRODUCTION_WRITE\n" in vps["canonical"].read_text(encoding="utf-8")   # greppable
    for name, unit in vps["sd"].units.items():
        assert unit["files"] == [(vps["canonical"], False)], name
    assert {p: p.read_bytes() for p in vps["old"]} == old_contents          # old files untouched
    backup = next(vps["backups"].iterdir())
    assert (backup / "manifest.json").is_file()
    if os.name != "nt":
        assert oct(vps["canonical"].stat().st_mode & 0o777) == "0o640"


def test_rollback_restores_the_previous_file_and_every_units_original_files(vps, capsys):
    original = vps["canonical"].read_bytes()
    assert run(vps, "apply") == consolidate.EXIT_OK
    backup = next(p for p in vps["backups"].iterdir() if not p.name.startswith("."))
    assert run(vps, "rollback", "--backup", str(backup)) == consolidate.EXIT_OK
    assert vps["canonical"].read_bytes() == original
    for name, unit in vps["sd"].units.items():
        assert unit["files"] == vps["sd"].original[name][0]
        assert not (vps["systemd_dir"] / f"{name}.service.d" / consolidate.DROPIN_NAME).exists()
    assert "ROLLED BACK" in capsys.readouterr().out


def test_a_name_two_readers_disagree_on_is_a_conflict_and_nothing_is_written(vps, capsys):
    web, runtime, _ = vps["old"]
    with runtime.open("a", encoding="utf-8") as fh:
        fh.write(f"MICROSOFT_GRAPH_CLIENT_SECRET=other-{SECRET}\n")
    original = vps["canonical"].read_bytes()
    assert run(vps, "apply") == consolidate.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "CONFLICT MICROSOFT_GRAPH_CLIENT_SECRET" in out and SECRET not in out
    assert vps["canonical"].read_bytes() == original and not vps["systemd_dir"].exists()


def test_a_difference_only_non_readers_hold_is_resolved_to_the_readers_value(vps, capsys):
    _, runtime, publisher = vps["old"]
    with publisher.open("a", encoding="utf-8") as fh:      # the publisher never reads SCHEDULER_CRON
        fh.write("SCHEDULER_CRON=0 5 * * *\n")
    assert run(vps, "plan") == consolidate.EXIT_OK
    assert "never read it" in capsys.readouterr().out


def test_a_reader_newly_seeing_a_name_is_refused_unless_accepted(vps, capsys):
    _, _, publisher = vps["old"]
    with publisher.open("a", encoding="utf-8") as fh:      # Django reads it; today it does not see it
        fh.write("PUBLISHER_BACKFILL_MAX_DAYS=10\n")
    assert run(vps, "plan") == consolidate.EXIT_REFUSED
    assert "CHANGE positive-moments:PUBLISHER_BACKFILL_MAX_DAYS: unset -> set" in capsys.readouterr().out
    assert run(vps, "plan", *[a for u in ("positive-moments", "positive-moments-media",
                                          "positive-moments-scheduler", "positive-moments-backfill")
                              for a in ("--accept-change", f"{u}:PUBLISHER_BACKFILL_MAX_DAYS")]) == consolidate.EXIT_OK


def test_prefer_resolves_a_conflict_and_the_per_unit_check_still_reports_it(vps, capsys):
    _, runtime, _ = vps["old"]
    with runtime.open("a", encoding="utf-8") as fh:
        fh.write("MICROSOFT_GRAPH_SCOPE=https://graph.microsoft.com/.default\n")
    with vps["old"][0].open("a", encoding="utf-8") as fh:
        fh.write("MICROSOFT_GRAPH_SCOPE=other-scope\n")
    assert run(vps, "plan", "--prefer", "MICROSOFT_GRAPH_SCOPE=positive-moments-scheduler") == consolidate.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "CHANGE positive-moments:MICROSOFT_GRAPH_SCOPE: value differs" in out and "CONFLICT" not in out


def test_a_missing_critical_name_is_refused(vps, capsys):
    _, runtime, _ = vps["old"]
    runtime.write_text(runtime.read_text(encoding="utf-8").replace("QA_MODEL_API_KEY=", "XQA_MODEL_API_KEY="),
                       encoding="utf-8")
    web = vps["old"][0]
    web.write_text(web.read_text(encoding="utf-8").replace("QA_MODEL_API_KEY=", "XQA_MODEL_API_KEY="),
                   encoding="utf-8")
    assert run(vps, "plan") == consolidate.EXIT_REFUSED
    assert "MISSING positive-moments-scheduler:QA_MODEL_API_KEY" in capsys.readouterr().out


def test_the_publisher_database_url_must_survive(vps, capsys):
    _, _, publisher = vps["old"]
    publisher.write_text(publisher.read_text(encoding="utf-8").replace("PUBLISHER_DATABASE_URL=", "OLD_URL="),
                         encoding="utf-8")
    assert run(vps, "plan") == consolidate.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "PUBLISHER_DATABASE_URL would be empty" in out and SECRET not in out


def test_a_systemd_parse_disagreement_refuses(vps, capsys):
    vps["sd"].tamper = lambda path, values: (
        {k: v + "x" for k, v in values.items()} if path.name == "candidate.env" else values)
    assert run(vps, "plan") == consolidate.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "round trip (systemd)" in out and SECRET not in out


def test_an_application_loading_difference_refuses(vps, capsys):
    def probe(app, unit, env, salt):
        return {"settings": {"qa_model_name": str(hash((salt, len(env))))}}      # differs: more names after
    assert run(vps, "plan", probe=probe) == consolidate.EXIT_REFUSED
    assert "LOADING" in capsys.readouterr().out


def test_apply_refuses_inside_the_cairo_scheduler_window(vps, capsys):
    assert run(vps, "apply", host=FakeHost(hour_utc=19)) == consolidate.EXIT_REFUSED      # 21:00 Cairo
    assert "scheduler window" in capsys.readouterr().out and not vps["systemd_dir"].exists()


def test_a_failure_after_install_rolls_everything_back(vps, capsys, monkeypatch):
    original = vps["canonical"].read_bytes()
    real_reload = vps["sd"].daemon_reload
    calls = {"n": 0}

    def broken_reload():
        calls["n"] += 1
        if calls["n"] == 1:
            return None                    # the drop-ins are written but systemd "ignores" them
        return real_reload()
    vps["sd"].daemon_reload = broken_reload
    assert run(vps, "apply") == consolidate.EXIT_ROLLED_BACK
    out = capsys.readouterr().out
    assert "APPLY FAILED" in out and "ROLLED BACK" in out and SECRET not in out
    assert vps["canonical"].read_bytes() == original


def test_values_that_neither_parser_agrees_on_are_refused_not_written():
    assert consolidate.render_value("A", "plain-value_1.2:3") == "A=plain-value_1.2:3"
    assert consolidate.render_value("A", "has space #and $dollar") == "A='has space #and $dollar'"
    assert consolidate.render_value("A", "") == "A="
    with pytest.raises(consolidate.Refused) as exc:
        consolidate.render_value("A", "it's")
    assert "it's" not in str(exc.value)


def test_the_real_probe_builds_every_config_without_files_network_or_database():
    env = {"DATABASE_URL": "postgresql://u:p@127.0.0.1:9/none", "MICROSOFT_GRAPH_TENANT_ID": "t",
           "PUBLISHER_DATABASE_URL": "postgresql://u:p@127.0.0.1:9/none", "PUBLISHER_START_DATE": "2026-10-23"}
    salt = secrets.token_hex(8)
    qa = consolidate.run_probe(REPO, "positive-moments", env, salt)
    assert set(qa) == {"settings", "scheduler", "n8n", "django"}
    assert all("__error__" not in fields for fields in qa.values()), qa
    assert qa == consolidate.run_probe(REPO, "positive-moments", dict(env), salt)
    changed = consolidate.run_probe(REPO, "positive-moments", {**env, "QA_MODEL_NAME": "other"}, salt)
    assert changed["settings"]["qa_model_name"] != qa["settings"]["qa_model_name"]
    publisher = consolidate.run_probe(REPO, "bunny-publisher", env, salt)
    assert set(publisher) == {"publisher", "publisher_problems"}


# --- the production layout (names as on the VPS; every value synthetic) ---------------------------

PROD_WEB = """APTEM_DATABASE_URL BUNNY_STAGING_ACCOUNT_API_KEY BUNNY_STAGING_API_KEY BUNNY_STAGING_CDN_HOSTNAME
BUNNY_STAGING_LIBRARY_ID COMMUNICATION_CENTRE_DATABASE_URL CORS_ALLOWED_ORIGINS CREATOMATE_API_KEY
DASHBOARD_PASSWORD DASHBOARD_USERNAME DATABASE_URL DJANGO_ALLOWED_HOSTS INITIAL_TEST_MODE
KBC_LECTURE_CALENDAR_USER_UPN LMS_DATABASE_URL MEDIA_POSITIVE_MOMENTS_FOLDER_ITEM_ID MEDIA_SHAREPOINT_DRIVE_ID
MICROSOFT_CLIENT_ID_2 MICROSOFT_CLIENT_SECRET_2 MICROSOFT_GRAPH_BASE_URL MICROSOFT_GRAPH_CLIENT_ID
MICROSOFT_GRAPH_CLIENT_SECRET MICROSOFT_GRAPH_SCOPE MICROSOFT_GRAPH_TENANT_ID N8N_API_KEY N8N_BASE_URL
PUBLISHER_LMS_LIBRARY_ID PUBLISHER_LMS_PLAYBACK_ENABLED QA_MODEL_API_KEY SOCIAL_MEDIA_SERVICE_TOKEN
PUBLISHER_LMS_SERVICE_TOKENS MICROSOFT_SSO_REDIRECT_URI MICROSOFT_SSO_EMAIL_DOMAINS""".split()
PROD_RUNTIME = """APTEM_DATABASE_URL DATABASE_URL KBC_LECTURE_CALENDAR_USER_UPN MICROSOFT_GRAPH_BASE_URL
MICROSOFT_GRAPH_CLIENT_ID MICROSOFT_GRAPH_CLIENT_SECRET MICROSOFT_GRAPH_SCOPE MICROSOFT_GRAPH_TENANT_ID
N8N_API_KEY N8N_BASE_URL QA_MODEL_API_KEY SCHEDULER_ALLOW_GRAPH SCHEDULER_ALLOW_LEGACY_WRITES
SCHEDULER_ALLOW_PROVIDER SCHEDULER_CRON SCHEDULER_DISCOVER SCHEDULER_ENABLED SCHEDULER_LOOKBACK_DAYS
SCHEDULER_TIMEZONE""".split()
PROD_PUBLISHER = """APTEM_DATABASE_URL BUNNY_PRODUCTION_ACCOUNT_API_KEY BUNNY_PRODUCTION_API_KEY
BUNNY_PRODUCTION_LIBRARY_ID BUNNY_STAGING_ACCOUNT_API_KEY BUNNY_STAGING_API_KEY BUNNY_STAGING_CDN_HOSTNAME
BUNNY_STAGING_LIBRARY_ID KBC_LECTURE_CALENDAR_USER_UPN LMS_DATABASE_URL MICROSOFT_GRAPH_BASE_URL
MICROSOFT_GRAPH_CLIENT_ID MICROSOFT_GRAPH_CLIENT_SECRET MICROSOFT_GRAPH_SCOPE MICROSOFT_GRAPH_TENANT_ID
PUBLISHER_ALERT_DELIVERY_ENABLED PUBLISHER_ALERT_DESTINATION PUBLISHER_DATABASE_URL
PUBLISHER_DISCOVERY_INTERVAL_SECONDS PUBLISHER_DISCOVER_CALENDAR PUBLISHER_ENCODING_STALL_HOURS
PUBLISHER_END_DATE PUBLISHER_FULL_RECONCILE_AT PUBLISHER_JOB_LEASE_SECONDS PUBLISHER_LIGHT_RECONCILE_HOURS
PUBLISHER_LMS_TEST_PROGRAMMES PUBLISHER_LOOKBACK_DAYS PUBLISHER_MAX_IMPORTS_IN_FLIGHT
PUBLISHER_MAX_IMPORT_GENERATIONS PUBLISHER_MODE PUBLISHER_POLL_SECONDS PUBLISHER_PRODUCTION_WRITE_ENABLED
PUBLISHER_START_DATE PUBLISHER_WORKERS RECORDING_LINK_SEARCH_REGION""".split()
PROD_VALUES = {
    "SCHEDULER_CRON": "0 21,23 * * *", "SCHEDULER_TIMEZONE": "Africa/Cairo", "SCHEDULER_LOOKBACK_DAYS": "3",
    "SCHEDULER_ENABLED": "true", "SCHEDULER_DISCOVER": "true", "SCHEDULER_ALLOW_GRAPH": "true",
    "SCHEDULER_ALLOW_PROVIDER": "true", "SCHEDULER_ALLOW_LEGACY_WRITES": "true",
    "MICROSOFT_GRAPH_SCOPE": "https://graph.microsoft.com/.default",
    "MICROSOFT_GRAPH_BASE_URL": "https://graph.microsoft.com/v1.0",
    "PUBLISHER_MODE": "PRODUCTION_WRITE", "PUBLISHER_PRODUCTION_WRITE_ENABLED": "true",
    "PUBLISHER_START_DATE": "2026-10-23", "PUBLISHER_END_DATE": "", "PUBLISHER_FULL_RECONCILE_AT": "05:00",
    "PUBLISHER_DISCOVERY_INTERVAL_SECONDS": "300", "PUBLISHER_ENCODING_STALL_HOURS": "6",
    "PUBLISHER_JOB_LEASE_SECONDS": "900", "PUBLISHER_LIGHT_RECONCILE_HOURS": "6", "PUBLISHER_LOOKBACK_DAYS": "3",
    "PUBLISHER_MAX_IMPORTS_IN_FLIGHT": "2", "PUBLISHER_MAX_IMPORT_GENERATIONS": "3", "PUBLISHER_POLL_SECONDS": "5",
    "PUBLISHER_WORKERS": "2", "PUBLISHER_DISCOVER_CALENDAR": "true", "PUBLISHER_ALERT_DELIVERY_ENABLED": "false",
    "BUNNY_STAGING_LIBRARY_ID": "786045", "BUNNY_PRODUCTION_LIBRARY_ID": "786045",
    "PUBLISHER_LMS_LIBRARY_ID": "786045", "PUBLISHER_LMS_PLAYBACK_ENABLED": "true",
    "RECORDING_LINK_SEARCH_REGION": "",                      # blank on the VPS: the platform default
    "CORS_ALLOWED_ORIGINS": "https://positive-moments.example.invalid",
    "DJANGO_ALLOWED_HOSTS": "positive-moments.example.invalid",
    "MICROSOFT_SSO_REDIRECT_URI": "https://positive-moments.example.invalid/api/auth/sso/callback",
    "MICROSOFT_SSO_EMAIL_DOMAINS": "example.invalid",
}


def _prod_value(name):
    if name in PROD_VALUES:
        return PROD_VALUES[name]
    if name.endswith("DATABASE_URL"):
        return f"postgresql://u:{SECRET}@{name.lower().replace('_', '-')}.example.invalid/db"
    return f"{SECRET}-{name.lower()}"


@pytest.fixture
def prod(tmp_path):
    app = tmp_path / "app"
    files = {}
    for key, names in (("web", PROD_WEB), ("runtime", PROD_RUNTIME), ("publisher", PROD_PUBLISHER)):
        path = {"web": app / "backend" / "config" / ".env", "runtime": app / "backend" / "qa-core.runtime.env",
                "publisher": app / "backend" / "config" / "publisher.env"}[key]
        files[key] = _write(path, {name: _prod_value(name) for name in names})
    canonical = app / "backend" / ".env"
    _write(canonical, {"KBC_UNRELATED_FLAG": "1"})              # the VPS's small backend/.env
    base = "PYTHONPATH=/var/www/Positive-moments PATH=/var/www/Positive-moments/backend/venv/bin:/usr/bin:/bin"
    units = {
        "positive-moments": {"files": [(files["web"], False)], "environment": base},
        "positive-moments-media": {"files": [(files["web"], False)], "environment": base},
        "positive-moments-scheduler": {"files": [(files["runtime"], False)], "environment": base},
        "positive-moments-backfill": {"files": [(files["runtime"], False)], "environment": base},
        "bunny-publisher": {"files": [(files["publisher"], False)], "environment": base + " TZ=Africa/Cairo"},
    }
    systemd_dir = tmp_path / "systemd"
    sd = FakeSystemd(units, systemd_dir, canonical)
    sd.running["positive-moments"] = set(PROD_WEB) | {"PATH", "PYTHONPATH"}   # no DJANGO_SECRET_KEY live
    return {"app": app, "sd": sd, "systemd_dir": systemd_dir, "backups": tmp_path / "backups",
            "canonical": canonical, "old": tuple(files.values())}


def _refusals(out):
    return [line for line in out.splitlines() if line.startswith("REFUSED: ")]


def test_production_layout_refuses_only_the_genuine_changes(prod, capsys):
    snapshot = {p: p.read_bytes() for p in (*prod["old"], prod["canonical"])}
    assert run(prod, "plan", probe=real_probe) == consolidate.EXIT_REFUSED
    out = capsys.readouterr().out
    refused = _refusals(out)
    assert len(refused) == 3, refused
    assert any(r.startswith("REFUSED: MISSING positive-moments:DJANGO_SECRET_KEY: absent today and after "
                            "consolidation (the running process does not have it either") for r in refused)
    for unit in ("positive-moments-scheduler", "positive-moments-backfill"):
        assert any(r.startswith(f"REFUSED: CHANGE {unit}:LMS_DATABASE_URL: unset -> set changes what") and
                   "settings.lms_database_url" in r for r in refused)
    # Harmless: blank RECORDING_LINK_SEARCH_REGION equals the default; proven, not assumed.
    assert "positive-moments-scheduler:RECORDING_LINK_SEARCH_REGION: unset -> set, VERIFIED no effect" in out
    # The false positives of the deployed version are now harmless visibility, listed by name.
    assert re.search(r"visible only \(harmless: positive-moments-scheduler never reads these\).*DASHBOARD_PASSWORD", out)
    assert re.search(r"visible only \(harmless: positive-moments never reads these\).*SCHEDULER_CRON", out)
    assert not any("SCHEDULER_" in r or "DASHBOARD" in r or "CORS" in r for r in refused)
    assert SECRET not in out and "postgresql://" not in out
    assert {p: p.read_bytes() for p in snapshot} == snapshot and not prod["systemd_dir"].exists()


def test_a_non_default_recording_region_is_a_real_change_and_stays_refused(prod, capsys):
    publisher = prod["old"][2]
    publisher.write_text(publisher.read_text(encoding="utf-8").replace(
        "RECORDING_LINK_SEARCH_REGION=\n", "RECORDING_LINK_SEARCH_REGION=EUR\n"), encoding="utf-8")
    assert run(prod, "plan", probe=real_probe) == consolidate.EXIT_REFUSED
    refused = _refusals(capsys.readouterr().out)
    for unit in ("positive-moments", "positive-moments-media", "positive-moments-scheduler",
                 "positive-moments-backfill"):
        assert any(r.startswith(f"REFUSED: CHANGE {unit}:RECORDING_LINK_SEARCH_REGION") for r in refused), unit


def test_keep_unset_preserves_every_units_exact_configuration_through_apply_and_rollback(prod, capsys):
    before = consolidate.build_plan(prod["sd"], prod["app"], {}, set(), set()).before
    original = {p: p.read_bytes() for p in (*prod["old"], prod["canonical"])}
    args = ["--keep-unset", "positive-moments-scheduler:LMS_DATABASE_URL",
            "--keep-unset", "positive-moments-backfill:LMS_DATABASE_URL",
            "--accept-missing", "positive-moments:DJANGO_SECRET_KEY"]
    assert run(prod, "apply", *args, probe=real_probe) == consolidate.EXIT_OK
    out = capsys.readouterr().out
    assert "VERDICT: READY" in out and "APPLIED" in out and SECRET not in out and "postgresql://" not in out
    dropin = (prod["systemd_dir"] / "positive-moments-scheduler.service.d" / consolidate.DROPIN_NAME).read_text()
    assert "UnsetEnvironment=LMS_DATABASE_URL" in dropin and "KBC_ENV_KEEP_UNSET=LMS_DATABASE_URL" in dropin
    canonical_values = consolidate.dotenv_parse(prod["canonical"])
    for unit, reads in environment.UNIT_VARIABLES.items():          # exact, value by value
        live = dict(consolidate.read_unit(prod["sd"], unit).environment)
        live.update(canonical_values)
        for key in prod["sd"].show(unit, "UnsetEnvironment").split():
            live.pop(key, None)
        # Exact for every name the unit reads, except names the plan PROVED have no effect
        # (here: a blank RECORDING_LINK_SEARCH_REGION, identical to the code default).
        proven = set(re.findall(rf"^note: {re.escape(unit)}:([A-Z0-9_]+): unset -> set, VERIFIED no effect",
                                out, re.MULTILINE))
        assert proven <= {"RECORDING_LINK_SEARCH_REGION"}, proven
        assert {k: live.get(k) for k in reads - proven} == {k: before[unit].get(k) for k in reads - proven}, unit
    assert canonical_values["PUBLISHER_DATABASE_URL"] != canonical_values["DATABASE_URL"]
    assert canonical_values["PUBLISHER_MODE"] == "PRODUCTION_WRITE"
    assert {p: p.read_bytes() for p in prod["old"]} == {p: original[p] for p in prod["old"]}
    backup = next(p for p in prod["backups"].iterdir() if not p.name.startswith("."))
    if os.name != "nt":
        assert oct(backup.stat().st_mode & 0o777) == "0o700"
    assert run(prod, "rollback", "--backup", str(backup)) == consolidate.EXIT_OK
    assert prod["canonical"].read_bytes() == original[prod["canonical"]]
    for unit in environment.UNIT_VARIABLES:
        assert prod["sd"].show(unit, "UnsetEnvironment") == ""
        assert not (prod["systemd_dir"] / f"{unit}.service.d" / consolidate.DROPIN_NAME).exists()


def test_keep_unset_is_refused_for_a_name_the_unit_has_today(prod, capsys):
    assert run(prod, "plan", "--keep-unset", "positive-moments:LMS_DATABASE_URL") == consolidate.EXIT_REFUSED
    assert "the unit HAS LMS_DATABASE_URL today" in capsys.readouterr().out


def test_a_secret_supplied_by_an_unmodelled_mechanism_is_called_out(prod, capsys):
    prod["sd"].running["positive-moments"].add("DJANGO_SECRET_KEY")
    assert run(prod, "plan") == consolidate.EXIT_REFUSED
    assert "YET the running process has it" in capsys.readouterr().out


def test_a_secret_from_the_units_environment_line_is_modelled_and_kept(prod, capsys):
    """Environment= stays in the unit, so a secret supplied there is present before AND after."""
    web = prod["sd"].units["positive-moments"]
    web["environment"] += f" DJANGO_SECRET_KEY={SECRET}"
    prod["sd"].original["positive-moments"] = (web["files"], web["environment"])
    plan = consolidate.build_plan(prod["sd"], prod["app"], {}, set(), set())
    assert plan.before["positive-moments"]["DJANGO_SECRET_KEY"] == plan.after["positive-moments"]["DJANGO_SECRET_KEY"]
    assert not any("DJANGO_SECRET_KEY" in issue for issue in plan.issues)
    assert "DJANGO_SECRET_KEY" not in plan.candidate          # never copied into backend/.env


def test_keep_unset_names_are_never_refilled_from_backend_env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("LMS_DATABASE_URL=postgresql://u:p@lms.example.invalid/x\nOTHER_NAME=1\n", encoding="utf-8")
    monkeypatch.setattr(environment, "CANONICAL_ENV_FILE", path)
    monkeypatch.delenv("LMS_DATABASE_URL", raising=False)
    monkeypatch.delenv("OTHER_NAME", raising=False)
    monkeypatch.setenv(environment.KEEP_UNSET_VARIABLE, "LMS_DATABASE_URL")
    environment.load_canonical_env()
    assert "LMS_DATABASE_URL" not in os.environ and os.environ["OTHER_NAME"] == "1"
    assert "LMS_DATABASE_URL" not in environment.canonical_values()


# --- the per-unit registry matches the real consumers -------------------------------------------

def test_settings_fields_split_exactly_into_core_and_media():
    from dataclasses import fields

    from app.config.settings import Settings
    names = {f.name for f in fields(Settings)}
    assert environment.SETTINGS_CORE_FIELDS | environment.SETTINGS_MEDIA_FIELDS == names
    assert not environment.SETTINGS_CORE_FIELDS & environment.SETTINGS_MEDIA_FIELDS


def test_media_settings_are_consumed_only_by_the_media_runner_and_django_media_views():
    users = set()
    for path in [*REPO.joinpath("app").rglob("*.py"), *REPO.joinpath("backend").rglob("*.py")]:
        rel = path.relative_to(REPO).as_posix()
        if "/tests/" in rel or rel.startswith("backend/venv") or rel == "app/config/settings.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(re.search(rf"\.{f}\b", text) for f in environment.SETTINGS_MEDIA_FIELDS):
            users.add(rel)
    assert users <= {"app/media/factory.py", "backend/operations/media_views.py"}, users
    importers = {p.relative_to(REPO).as_posix() for p in [*REPO.joinpath("app").rglob("*.py"),
                                                         *REPO.joinpath("backend").rglob("*.py")]
                 if "/tests/" not in p.as_posix() and "venv" not in p.as_posix()
                 and "app.media.factory" in p.read_text(encoding="utf-8", errors="replace")}
    assert importers <= {"app/cli/media.py", "backend/operations/media_views.py"}, importers


def test_the_web_process_never_reads_the_scheduler_config_and_the_media_runner_no_n8n_or_scheduler():
    backend = [p for p in REPO.joinpath("backend").rglob("*.py") if "tests" not in p.parts and "venv" not in p.parts]
    for path in backend:
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "SchedulerConfig" not in text and "nightly_quiet_window" not in text \
            and "build_scheduler" not in text and "build_backfill_runner" not in text, path
    media = (REPO / "app" / "cli" / "media.py").read_text(encoding="utf-8")
    imports = "\n".join(l for l in media.splitlines() if re.match(r"\s*(from|import)\s", l))
    assert "n8n" not in imports and "scheduler" not in imports and "config.settings import Settings" in imports
    assert not environment.DJANGO_VARIABLES & (environment.UNIT_VARIABLES["positive-moments-scheduler"]
                                               - environment.SETTINGS_CORE_VARIABLES - environment.N8N_VARIABLES)
    assert "DATABASE_URL" not in environment.UNIT_VARIABLES["bunny-publisher"]
