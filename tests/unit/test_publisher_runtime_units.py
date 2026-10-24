"""P0-D publisher runtime: configuration, titles, library gate, scheduling rules, policy, client, boundaries."""
from __future__ import annotations

import io
import json
import os
import re
import socket
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from app.bunny.client import BunnyError
from app.bunny.identity import build_publisher_title, build_title, video_record_id
from app.bunny.verification import media_checks
from app.publisher.config import (
    PRODUCTION_WRITE, SHADOW, STAGING_WRITE, ConfigError, PublisherConfig,
)
from app.publisher.daemon import DISCOVERY, RECONCILE_FULL, RECONCILE_LIGHT, PublisherDaemon, last_full_boundary
from app.publisher.handlers import BunnyJobHandler, parse_subject, part_subject_key
from app.publisher.progress import EXCLUDE, INCLUDE, NEEDS_DECISION, NOT_LOCATED, part_policy
from app.publisher.reconcile import AlertDispatcher
from app.publisher.runtime import build_daemon, check_library_settings
from tests.unit.publisher_fakes import LIBRARY_ID, FakeBunnyLibrary, FakeCore, FakeMedia, library_settings
from tests.unit.recording_fakes import FakeRecordingGraph
from tests.unit.test_bunny_client import KEY, Script, client
from tests.unit.test_publisher_transcript_worker import JOB, LID, _Conn, _Queue

REPO = Path(__file__).resolve().parents[2]
BASE = {"DATABASE_URL": "postgresql://user:SECRET-DB-PASSWORD@db.invalid/x", "APTEM_DATABASE_URL": "postgresql://a",
        "LMS_DATABASE_URL": "postgresql://l", "MICROSOFT_GRAPH_TENANT_ID": "t", "MICROSOFT_GRAPH_CLIENT_ID": "c",
        "MICROSOFT_GRAPH_CLIENT_SECRET": "SECRET-GRAPH", "KBC_LECTURE_CALENDAR_USER_UPN": "cal@example.invalid",
        "PUBLISHER_START_DATE": "2026-11-01"}


def cfg(**env) -> PublisherConfig:
    return PublisherConfig.from_mapping({**BASE, **env})


# -- configuration ---------------------------------------------------------------------------

def test_shadow_is_the_default_and_needs_no_bunny_credentials():
    config = cfg()
    assert config.mode == SHADOW and not config.writes_bunny and config.library_id is None
    assert config.problems() == []


def test_staging_write_needs_its_library_key_and_account_key():
    problems = cfg(PUBLISHER_MODE="staging_write").problems()
    assert any("BUNNY_STAGING_LIBRARY_ID" in p for p in problems)
    assert any("ACCOUNT_API_KEY" in p for p in problems)
    ok = cfg(PUBLISHER_MODE="STAGING_WRITE", BUNNY_STAGING_LIBRARY_ID="786045", BUNNY_STAGING_API_KEY="k",
             BUNNY_STAGING_ACCOUNT_API_KEY="a")
    assert ok.problems() == [] and ok.library_id == 786045 and ok.writes_bunny


def test_production_write_is_disabled_by_default():
    config = cfg(PUBLISHER_MODE=PRODUCTION_WRITE, BUNNY_PRODUCTION_LIBRARY_ID="900001", BUNNY_PRODUCTION_API_KEY="k",
                 BUNNY_PRODUCTION_ACCOUNT_API_KEY="a")
    assert any("PRODUCTION_WRITE is disabled" in p for p in config.problems())
    # Library 786045 is adopted as the permanent production library (2026-10-24): the production
    # library MAY equal the staging library, and the mode then writes to that one library.
    enabled = cfg(PUBLISHER_MODE=PRODUCTION_WRITE, BUNNY_PRODUCTION_LIBRARY_ID="900001", BUNNY_PRODUCTION_API_KEY="k",
                  BUNNY_PRODUCTION_ACCOUNT_API_KEY="a", PUBLISHER_PRODUCTION_WRITE_ENABLED="true",
                  BUNNY_STAGING_LIBRARY_ID="900001")
    assert enabled.problems() == [] and enabled.library_id == 900001 and enabled.describe()["adopts_staging_library"]
    # CDN token authentication is DEFERRED by default: no key is needed, and a configured key is ignored.
    assert enabled.cdn_token_auth_policy == "DEFERRED" and enabled.cdn_signing_key == ""
    assert enabled.approved_referrers == ("lms.kentbusinesscollege.org",) and enabled.require_block_none_referrer
    ignored = cfg(PUBLISHER_MODE=PRODUCTION_WRITE, BUNNY_PRODUCTION_LIBRARY_ID="900001", BUNNY_PRODUCTION_API_KEY="k",
                  BUNNY_PRODUCTION_ACCOUNT_API_KEY="a", PUBLISHER_PRODUCTION_WRITE_ENABLED="true",
                  BUNNY_CDN_TOKEN_KEY="cdn-key")
    assert ignored.problems() == [] and ignored.cdn_signing_key == "" and ignored.describe()["cdn_token_key_ignored"]
    assert "cdn-key" not in repr(ignored) and "cdn-key" not in str(ignored.describe())
    # REQUIRED (future hardening) needs the key and the CDN host, and then signs.
    required = cfg(PUBLISHER_MODE=PRODUCTION_WRITE, BUNNY_PRODUCTION_LIBRARY_ID="900001", BUNNY_PRODUCTION_API_KEY="k",
                   BUNNY_PRODUCTION_ACCOUNT_API_KEY="a", PUBLISHER_PRODUCTION_WRITE_ENABLED="true",
                   PUBLISHER_CDN_TOKEN_AUTH="required")
    assert any("PUBLISHER_CDN_TOKEN_AUTH=REQUIRED needs" in p for p in required.problems())
    signed = cfg(PUBLISHER_MODE=PRODUCTION_WRITE, BUNNY_PRODUCTION_LIBRARY_ID="900001", BUNNY_PRODUCTION_API_KEY="k",
                 BUNNY_PRODUCTION_ACCOUNT_API_KEY="a", PUBLISHER_PRODUCTION_WRITE_ENABLED="true",
                 PUBLISHER_CDN_TOKEN_AUTH="REQUIRED", BUNNY_CDN_TOKEN_KEY="cdn-key",
                 BUNNY_PRODUCTION_CDN_HOSTNAME="cdn.example.invalid")
    assert signed.problems() == [] and signed.cdn_signing_key == "cdn-key"
    assert any("PUBLISHER_CDN_TOKEN_AUTH must be one of" in p
               for p in cfg(PUBLISHER_MODE="SHADOW", PUBLISHER_CDN_TOKEN_AUTH="sometimes").problems())
    relaxed = cfg(PUBLISHER_MODE="SHADOW", PUBLISHER_APPROVED_REFERRERS=" LMS.KentBusinessCollege.org , b.example ",
                  PUBLISHER_REQUIRE_BLOCK_NONE_REFERRER="false")
    assert relaxed.approved_referrers == ("b.example", "lms.kentbusinesscollege.org")
    assert relaxed.require_block_none_referrer is False


def test_the_old_library_is_refused_in_every_mode():
    for mode in (SHADOW, STAGING_WRITE):
        assert any("785080" in p for p in cfg(PUBLISHER_MODE=mode, BUNNY_STAGING_LIBRARY_ID="785080").problems())


def test_missing_scope_and_alert_delivery_are_refused():
    problems = PublisherConfig.from_mapping({**BASE, "PUBLISHER_START_DATE": "",
                                             "PUBLISHER_ALERT_DELIVERY_ENABLED": "true"}).problems()
    assert any("PUBLISHER_START_DATE" in p for p in problems)
    assert any("alert delivery is not enabled" in p for p in problems)
    with pytest.raises(ConfigError):
        PublisherConfig.from_mapping({**BASE, "PUBLISHER_START_DATE": "", "PUBLISHER_MODE": "x"}).validate()


def test_secrets_never_appear_in_repr_or_describe():
    config = cfg(PUBLISHER_MODE="STAGING_WRITE", BUNNY_STAGING_LIBRARY_ID="786045", BUNNY_STAGING_API_KEY="SECRET-BUNNY",
                 BUNNY_STAGING_ACCOUNT_API_KEY="SECRET-ACCOUNT")
    text = repr(config) + json.dumps(config.describe(), default=str)
    for secret in ("SECRET-DB-PASSWORD", "SECRET-GRAPH", "SECRET-BUNNY", "SECRET-ACCOUNT", "postgresql://"):
        assert secret not in text


def test_validate_config_cli_prints_no_secret(capsys, monkeypatch):
    from app.cli import publisher as cli
    # Settings.from_environment() (other tests) loads backend/.env into os.environ: start clean.
    for key in list(os.environ):
        if key.startswith(("BUNNY_", "PUBLISHER_")):
            monkeypatch.delenv(key)
    for key, value in {**BASE, "PUBLISHER_MODE": "STAGING_WRITE", "BUNNY_STAGING_API_KEY": "SECRET-BUNNY"}.items():
        monkeypatch.setenv(key, value)
    code = cli.main(["validate-config"])
    out = capsys.readouterr().out
    assert code == 2 and "BUNNY_STAGING_LIBRARY_ID" in out
    assert "SECRET" not in out and "postgresql://" not in out


# -- identity ----------------------------------------------------------------------------------

def test_publisher_title_carries_the_lms_occurrence_and_the_part():
    title = build_publisher_title("AI &amp; Project | Control", date(2031, 1, 2), "OCC-123", "rec-0a1b2c3d",
                                  part_index=2, part_count=3)
    assert title == "AI & Project Control | 2031-01-02 | lms OCC-123 | part 2/3 | rec-0a1b2c3d"
    assert "part" not in build_publisher_title("X", "2031-01-02", "OCC-1", "rec-0a1b2c3d")
    assert len(build_publisher_title("y" * 500, "2031-01-02", "OCC-1", "rec-0a1b2c3d")) == 200
    for bad in ({"occurrence_id": "a b"}, {"occurrence_id": "https://x"}, {"part_index": 4}):
        with pytest.raises(ValueError):
            build_publisher_title("X", "2031-01-02", bad.get("occurrence_id", "OCC"), "rec-0a1b2c3d",
                                  part_index=bad.get("part_index", 1), part_count=3)


def test_generation_one_is_the_pilots_record_id_and_later_generations_differ():
    assert video_record_id(LIBRARY_ID, "d", "i") == video_record_id(LIBRARY_ID, "d", "i", 1)
    assert len({video_record_id(LIBRARY_ID, "d", "i", g) for g in (1, 2, 3)}) == 3
    assert "rec-0a1b2c3d" in build_title("X", "2031-01-02", "bb2d676b-0000-0000-0000-000000000000", "rec-0a1b2c3d")


# -- library gate and media verification ---------------------------------------------------------

def test_library_settings_gate():
    assert check_library_settings(library_settings()) == []
    assert check_library_settings(library_settings(EncodingTier="Premium"))
    assert check_library_settings(library_settings(JitEncodingEnabled=True))
    assert check_library_settings(library_settings(EnableTranscribing=True))
    assert check_library_settings(library_settings(TranscribingCaptionLanguages=["en"]))


def _write_config():
    return cfg(PUBLISHER_MODE="STAGING_WRITE", BUNNY_STAGING_LIBRARY_ID=str(LIBRARY_ID), BUNNY_STAGING_API_KEY="k",
               BUNNY_STAGING_ACCOUNT_API_KEY="a", PUBLISHER_DISCOVER_CALENDAR="false")


def test_build_daemon_refuses_a_premium_or_transcribing_library():
    graph = FakeRecordingGraph()
    bunny = FakeBunnyLibrary(FakeMedia(graph.items))
    with pytest.raises(ConfigError, match="AI transcribing"):
        build_daemon(_write_config(), graph=graph, bunny=bunny, core=FakeCore(EnableTranscribing=True),
                     lms_source=object(), connect=lambda: None)
    daemon = build_daemon(_write_config(), graph=graph, bunny=bunny, core=FakeCore(), lms_source=object(),
                          connect=lambda: None)
    assert set(daemon.kinds) == {"ACQUIRE_TRANSCRIPT", "RESOLVE_RECORDING", "IMPORT_VIDEO", "POLL_ENCODING",
                                 "PUBLISH_CAPTION"}


def test_build_daemon_in_shadow_registers_no_bunny_work_and_makes_no_bunny_call():
    core = FakeCore()
    daemon = build_daemon(cfg(PUBLISHER_DISCOVER_CALENDAR="false"), graph=FakeRecordingGraph(), core=core,
                          lms_source=object(), connect=lambda: None)
    assert daemon.kinds == ["ACQUIRE_TRANSCRIPT", "RESOLVE_RECORDING"] and core.calls == 0


def test_media_checks_match_the_pilot_rules():
    video = {"length": 7941, "outputCodecs": "x264", "availableResolutions": "360p,480p,720p,1080p",
             "transcodingMessages": []}
    checks, failed = media_checks(video, {"configuredResolutions": ["360p", "480p", "720p", "1080p", "1440p"]}, {},
                                  source_duration_seconds=7941.6, source_height=1080, encoding_tier="Free",
                                  duration_tolerance_seconds=2.0)
    assert failed == [] and checks["expected_resolutions_present"]
    _, failed = media_checks({**video, "length": 3000}, {"configuredResolutions": ["720p"]}, {},
                             source_duration_seconds=7941.6, source_height=None, encoding_tier="Premium",
                             duration_tolerance_seconds=2.0)
    assert failed == ["length_within_tolerance", "encoding_tier_free"]


# -- Bunny delete ---------------------------------------------------------------------------------

def test_delete_video_treats_404_as_already_absent_and_timeouts_as_uncertain():
    gone = Script((404, {"message": "not found"}))
    assert client(gone).delete_video("vid") == {"deleted": False, "already_absent": True, "http_status": 404}
    assert gone.requests[0].get_method() == "DELETE" and gone.requests[0].get_header("Accesskey") == KEY
    assert client(Script((200, {"success": True}))).delete_video("vid")["deleted"]
    with pytest.raises(BunnyError) as error:
        client(Script(socket.timeout("t"))).delete_video("vid")
    assert error.value.uncertain


# -- scheduling ---------------------------------------------------------------------------------

def test_full_reconciliation_boundary_is_0500_cairo():
    at = time(5, 0)
    # 2031-01-02 02:59 UTC = 04:59 Cairo -> the boundary is yesterday's 05:00 Cairo (03:00 UTC)
    assert last_full_boundary(datetime(2031, 1, 2, 2, 59, tzinfo=timezone.utc), at) == \
        datetime(2031, 1, 1, 3, 0, tzinfo=timezone.utc)
    assert last_full_boundary(datetime(2031, 1, 2, 3, 0, tzinfo=timezone.utc), at) == \
        datetime(2031, 1, 2, 3, 0, tzinfo=timezone.utc)


class _Schedule:
    def __init__(self, rows):
        self.rows = rows

    def all(self, connection):
        return self.rows


def _daemon(rows):
    daemon = PublisherDaemon(config=cfg(), connect=lambda: None, handlers={}, planner=None, reconciler=None)
    daemon.schedule = _Schedule(rows)
    return daemon


def test_due_tasks_after_a_fresh_start_and_after_a_day_offline():
    now = datetime(2031, 1, 2, 9, 0, tzinfo=timezone.utc)
    assert _daemon([]).due_tasks(None, now) == [RECONCILE_FULL, DISCOVERY]
    recent = [{"task": DISCOVERY, "last_started_at": now - timedelta(minutes=2), "last_succeeded_at": now},
              {"task": RECONCILE_LIGHT, "last_started_at": now - timedelta(hours=1), "last_succeeded_at": now},
              {"task": RECONCILE_FULL, "last_started_at": now - timedelta(hours=5),
               "last_succeeded_at": now - timedelta(hours=5)}]
    assert _daemon(recent).due_tasks(None, now) == []
    offline = [{**r, "last_started_at": r["last_started_at"] - timedelta(days=1),
                "last_succeeded_at": r["last_succeeded_at"] - timedelta(days=1)} for r in recent]
    assert _daemon(offline).due_tasks(None, now) == [RECONCILE_FULL, DISCOVERY]


# -- policy, handlers, alerts ----------------------------------------------------------------------

def test_part_policy_never_drops_or_adds_a_part_on_its_own():
    located = {"locate_status": "LOCATED", "classification": "FULL", "transcript_artifact_id": None}
    assert part_policy(located, None) == INCLUDE
    assert part_policy({**located, "classification": "FRAGMENT"}, None) == NEEDS_DECISION
    assert part_policy({**located, "classification": "FRAGMENT", "transcript_artifact_id": "a"}, None) == INCLUDE
    assert part_policy({**located, "classification": "FRAGMENT"}, "EXCLUDE") == EXCLUDE
    assert part_policy({**located, "locate_status": "NOT_FOUND"}, None) == NOT_LOCATED


def test_part_subjects_round_trip():
    assert parse_subject(part_subject_key(LID, 3)) == (LID, 3)
    assert parse_subject(f"lecture:{LID}") == (LID, None)
    with pytest.raises(ValueError):
        parse_subject("video:x")


class _BunnyWorker:
    def __init__(self, outcome):
        self.outcome, self.seen = outcome, []

    def run(self, lecture_id, part, *, attempt, operator=False):
        self.seen.append((lecture_id, part, attempt, operator))
        return self.outcome


class _FollowQueue(_Queue):
    def enqueue(self, c, **kw):
        self.calls.append(("enqueue", kw["job_kind"], kw["subject_key"]))
        return {}, True


def test_bunny_handler_enqueues_the_next_track_with_the_transition():
    from app.publisher.bunny_jobs import DONE, POLL_ENCODING, JobOutcome
    queue = _FollowQueue()
    worker = _BunnyWorker(JobOutcome(LID, DONE, "VIDEO_IDENTIFIED", follow_ups=[(POLL_ENCODING, 2)]))
    BunnyJobHandler("IMPORT_VIDEO", worker=worker, connect=_Conn, queue=queue).handle(
        {**JOB, "subject_key": part_subject_key(LID, 2), "payload": {"operator_retry": True}}, "w1")
    assert worker.seen == [(LID, 2, 3, True)]
    assert queue.calls == [("succeed",), ("event", "IMPORT_DONE", LID),
                           ("enqueue", "POLL_ENCODING", part_subject_key(LID, 2))]


def test_a_reviewed_bunny_step_schedules_nothing():
    from app.publisher.bunny_jobs import POLL_ENCODING, REVIEW, JobOutcome
    queue = _FollowQueue()
    worker = _BunnyWorker(JobOutcome(LID, REVIEW, "CLEANUP_FAILED", follow_ups=[(POLL_ENCODING, 1)]))
    BunnyJobHandler("POLL_ENCODING", worker=worker, connect=_Conn, queue=queue).handle(
        {**JOB, "subject_key": part_subject_key(LID, 1)}, "w1")
    assert queue.calls == [("review", "CLEANUP_FAILED"), ("event", "ENCODING_REVIEW_REQUIRED", LID)]


def test_alert_rendering_and_notification_status_never_send():
    dispatcher = AlertDispatcher(cfg(PUBLISHER_ALERT_DESTINATION="ops-mailbox"))
    assert dispatcher.notification_status == "DISABLED"
    assert AlertDispatcher(cfg()).notification_status == "NOT_SENT"
    text = AlertDispatcher.render({"severity": "CRITICAL", "kind": "HEARTBEAT_STALE", "lecture_id": None,
                                   "summary": "worker w last seen"})
    assert text == "[CRITICAL] HEARTBEAT_STALE: worker w last seen"
    digest = AlertDispatcher.render_digest({"missing_caption": 2, "kind": "FULL", "nested": {"x": 1}}, "2031-01-02")
    assert "missing_caption: 2" in digest and "nested" not in digest


# -- boundaries and deployment files ---------------------------------------------------------------

def test_publisher_runtime_imports_no_qa_writer_model_or_settings_code():
    env = {k: v for k, v in os.environ.items()
           if not any(s in k.upper() for s in ("OPENAI", "QA_MODEL", "N8N", "DATABASE_URL"))}
    probe = (
        "import sys, app.cli.publisher, app.publisher.runtime, app.publisher.daemon, app.publisher.planner,"
        " app.publisher.reconcile, app.publisher.operations, app.publisher.bunny_jobs, app.publisher.pilot_migration\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] == 'openai' or m.startswith(("
        "'app.qa', 'app.writer', 'app.rendering', 'app.positive_moments', 'app.engagement', 'app.attendance',"
        "'app.orchestration.orchestrator', 'app.orchestration.runner', 'app.orchestration.n8n_preflight',"
        "'app.orchestration.scheduler', 'app.orchestration.daemon', 'app.config.settings')))\n"
        "print(bad)\n")
    result = subprocess.run([sys.executable, "-c", probe], cwd=REPO, env=env, capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_the_service_unit_is_prepared_with_the_vps_conventions():
    unit = (REPO / "deploy" / "systemd" / "bunny-publisher.service").read_text(encoding="utf-8")
    directives = "\n".join(line for line in unit.splitlines() if not line.strip().startswith("#"))
    assert "ExecStart=/var/www/Positive-moments/backend/venv/bin/python -m app.cli.publisher run" in directives
    assert "ExecStartPre=/var/www/Positive-moments/backend/venv/bin/python -m app.cli.publisher validate-config" \
        in directives
    assert "EnvironmentFile=/var/www/Positive-moments/backend/config/publisher.env" in directives
    assert "User=www-data" in directives and "KillSignal=SIGTERM" in directives
    assert "RestartPreventExitStatus=2" in directives and "NoNewPrivileges=true" in directives
    assert "PUBLISHER_MODE" not in directives                    # the mode comes only from the env file


def test_the_env_template_holds_no_values_for_secrets_and_defaults_to_shadow():
    text = (REPO / "deploy" / "publisher.env.template").read_text(encoding="utf-8")
    values = dict(line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#"))
    assert values["PUBLISHER_MODE"] == "SHADOW" and values["PUBLISHER_PRODUCTION_WRITE_ENABLED"] == "false"
    assert values["PUBLISHER_ALERT_DELIVERY_ENABLED"] == "false"
    for name, value in values.items():
        if re.search(r"KEY|SECRET|DATABASE_URL", name):
            assert value == "", name


def test_migration_028_is_additive_and_has_a_guarded_rollback():
    proposed = REPO / "app" / "db" / "migrations_proposed"
    text = (proposed / "028_create_publisher_runtime_tables.sql").read_text(encoding="utf-8")
    code = re.sub(r"\bON (DELETE|UPDATE)\b", "", re.sub(r"'[^']*'", "''", re.sub(r"--[^\n]*", "", text)))
    assert not re.search(r"\b(ALTER|DROP|TRUNCATE|DELETE|UPDATE|INSERT)\b", code)
    assert set(re.findall(r"CREATE TABLE IF NOT EXISTS public\.([a-z_]+)", text)) == {
        "bunny_publisher_part_decisions", "bunny_publisher_schedule", "bunny_publisher_alerts", "bunny_publisher_reconciliation_runs"}
    rollback = (proposed / "028_create_publisher_runtime_tables.rollback.sql").read_text(encoding="utf-8")
    assert "RAISE EXCEPTION" in rollback


# -- P0-G: the publisher's own DIRECT database connection ----------------------------------------
POOLED = "postgresql://u:SECRET-DB-PASSWORD@ep-calm-sky-a1b2c3-pooler.eu-central-1.aws.neon.tech/li?sslmode=require"
DIRECT = "postgresql://u:SECRET-DB-PASSWORD@ep-calm-sky-a1b2c3.eu-central-1.aws.neon.tech/li?sslmode=require"


def test_a_pooled_neon_endpoint_is_refused_and_the_direct_one_accepted():
    from app.publisher.config import database_endpoint
    assert (database_endpoint(POOLED), database_endpoint(DIRECT), database_endpoint(""),
            database_endpoint("postgresql://u@127.0.0.1:55432/x")) == ("neon-pooled", "neon-direct", "missing", "other")
    pooled = cfg(PUBLISHER_DATABASE_URL=POOLED)
    assert any("DIRECT endpoint" in p for p in pooled.problems())
    direct = cfg(PUBLISHER_DATABASE_URL=DIRECT)
    assert not any("DIRECT endpoint" in p for p in direct.problems())
    assert direct.describe()["database_endpoint"] == "neon-direct"
    assert "SECRET" not in json.dumps(direct.describe()) and "neon.tech" not in json.dumps(direct.describe())


def test_the_publisher_url_wins_over_the_generic_pooled_name_and_the_shell(tmp_path, monkeypatch):
    assert cfg(DATABASE_URL=POOLED, PUBLISHER_DATABASE_URL=DIRECT).database_url == DIRECT
    env_file = tmp_path / "publisher.env"
    env_file.write_text(f"PUBLISHER_DATABASE_URL={DIRECT}\nPUBLISHER_START_DATE=2026-10-21\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", POOLED)                 # a pooled URL left in the shell
    monkeypatch.setenv("PUBLISHER_DATABASE_URL", POOLED)
    config = PublisherConfig.from_environment(str(env_file))
    assert config.database_url == DIRECT and config.start_date == date(2026, 10, 21)


def test_end_date_scopes_the_planner_window_to_one_day():
    from app.publisher.planner import Planner
    config = cfg(PUBLISHER_START_DATE="2026-10-21", PUBLISHER_END_DATE="2026-10-21", PUBLISHER_LOOKBACK_DAYS="14")
    planner = Planner(config=config, connect=None)
    for now in (datetime(2026, 10, 21, 20, tzinfo=timezone.utc), datetime(2026, 10, 30, 9, tzinfo=timezone.utc)):
        assert planner.window(now) == (date(2026, 10, 21), date(2026, 10, 21))
    assert any("END_DATE" in p for p in cfg(PUBLISHER_START_DATE="2026-10-21",
                                            PUBLISHER_END_DATE="2026-10-20").problems())


def test_db_smoke_refuses_a_pooled_url_without_connecting(capsys, monkeypatch):
    from app.cli import publisher as cli
    from app.publisher.smoke import run_db_smoke
    report = run_db_smoke(POOLED)
    assert report["ok"] is False and "POOLED" in report["refused"] and not report["checks"]
    for key in list(os.environ):
        if key.startswith(("BUNNY_", "PUBLISHER_")) or key == "DATABASE_URL":
            monkeypatch.delenv(key)
    monkeypatch.setenv("PUBLISHER_DATABASE_URL", POOLED)
    assert cli.main(["db-smoke"]) == 2
    out = capsys.readouterr().out
    assert "SECRET" not in out and "neon.tech" not in out and "postgresql://" not in out


def test_the_env_template_names_only_the_publishers_own_database_url():
    text = (REPO / "deploy" / "publisher.env.template").read_text(encoding="utf-8")
    keys = {line.split("=", 1)[0] for line in text.splitlines() if line and not line.startswith("#")}
    assert "PUBLISHER_DATABASE_URL" in keys and "DATABASE_URL" not in keys and "PUBLISHER_END_DATE" in keys


# -- P1: continuous production -------------------------------------------------------------------
def test_continuous_production_window_follows_cairo_today_with_lookback():
    """No END_DATE: the window rolls with the Cairo calendar day, floored at the go-live START_DATE."""
    from app.publisher.planner import Planner
    planner = Planner(config=cfg(PUBLISHER_START_DATE="2026-10-23", PUBLISHER_LOOKBACK_DAYS="3"), connect=None)
    # 22:30 UTC on 24 Oct is already 25 Oct in Cairo (UTC+3)
    assert planner.window(datetime(2026, 10, 24, 22, 30, tzinfo=timezone.utc)) == (date(2026, 10, 23), date(2026, 10, 25))
    assert planner.window(datetime(2026, 11, 2, 9, tzinfo=timezone.utc)) == (date(2026, 10, 30), date(2026, 11, 2))


def test_every_publisher_database_connection_is_bounded(monkeypatch):
    from app.publisher import runtime
    from app.publisher.config import DB_CONNECT_KWARGS
    seen = {}
    monkeypatch.setattr(runtime.psycopg, "connect", lambda url, **kw: seen.update(kw) or "conn")
    assert runtime.connector(cfg())() == "conn"
    assert seen["prepare_threshold"] is None
    for key in ("connect_timeout", "keepalives", "keepalives_idle", "keepalives_interval", "keepalives_count",
                "tcp_user_timeout"):
        assert seen[key] == DB_CONNECT_KWARGS[key]
    assert 0 < DB_CONNECT_KWARGS["connect_timeout"] <= 30 and DB_CONNECT_KWARGS["keepalives"] == 1


def test_publisher_discovery_runs_name_their_runner_and_mode():
    from app.publisher.planner import PublisherDiscoveryRunRepository

    class Inner:
        def start(self, connection, target_date):
            self.started = target_date
            return "run-1"

        def complete(self, connection, run_id, summary):
            self.completed = (run_id, summary)

    inner = Inner()
    runs = PublisherDiscoveryRunRepository(inner, "STAGING_WRITE")
    assert runs.start(None, date(2026, 10, 24)) == "run-1"
    runs.complete(None, "run-1", {"status": "COMPLETED", "metadata": {"canonical_lecture_candidates": 12}})
    run_id, summary = inner.completed
    assert summary["status"] == "COMPLETED" and summary["metadata"] == {
        "canonical_lecture_candidates": 12, "runner": "bunny_publisher", "publisher_mode": "STAGING_WRITE"}
