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
        self.original = {name: list(unit["files"]) for name, unit in units.items()}
        self.tamper = None

    def show(self, unit, prop):
        u = self.units.get(unit)
        if prop == "LoadState":
            return "loaded" if u else "not-found"
        if prop == "EnvironmentFiles":
            return "\n".join(f"{p} (ignore_errors={'yes' if i else 'no'})" for p, i in u["files"])
        return {"Environment": u.get("environment", ""), "User": "www-data",
                "ExecStart": "python -m app.cli.x run", "ExecStartPre": ""}[prop]

    def parse_env_file(self, path):
        values = consolidate.dotenv_parse(Path(path))
        return self.tamper(Path(path), values) if self.tamper else values

    def daemon_reload(self):
        for name, unit in self.units.items():
            dropin = self.systemd_dir / f"{name}.service.d" / consolidate.DROPIN_NAME
            if dropin.is_file() and "EnvironmentFile=\n" in dropin.read_text(encoding="utf-8"):
                path = dropin.read_text(encoding="utf-8").split("EnvironmentFile=\nEnvironmentFile=")[1].strip()
                unit["files"] = [(Path(path), False)]
            else:
                unit["files"] = list(self.original[name])


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
    """Stands in for the child-process probe: a digest of exactly the names the unit reads."""
    reads = environment.UNIT_VARIABLES[unit]
    return {"config": {k: str(hash((salt, env.get(k)))) for k in sorted(reads)}}


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
        assert unit["files"] == vps["sd"].original[name]
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
