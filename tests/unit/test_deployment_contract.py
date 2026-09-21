"""
Phase 4C2 static checks: the production deployment contract.

There is no VPS in a test suite, so these do not prove the scheduler runs.
They protect the handful of properties that are easy to break in a one-line
edit and expensive to discover at 21:00 Cairo on a server nobody is watching:
what goes into the image, what the container may reach, and what stopping it
does.
"""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEDULER = ROOT / "automation" / "scheduler"
COMPOSE = (SCHEDULER / "docker-compose.yml").read_text(encoding="utf-8")
DOCKERFILE = (SCHEDULER / "Dockerfile").read_text(encoding="utf-8")
REQUIREMENTS = (SCHEDULER / "requirements.txt").read_text(encoding="utf-8")


def directives(text: str) -> str:
    """
    The file with its comments removed.

    These files explain themselves at length, and several of the things the
    comments DISCUSS are exactly the things the assertions below forbid. A
    check that reads the prose reports a problem that is not there.
    """
    return "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("#"))


COMPOSE_DIRECTIVES = directives(COMPOSE)
DOCKERFILE_DIRECTIVES = directives(DOCKERFILE)
REQUIREMENTS_DIRECTIVES = directives(REQUIREMENTS)


# --- what the image contains --------------------------------------------------

def test_the_production_image_installs_production_dependencies_only():
    """
    A scheduler that ships Django, DRF and pytest is a larger attack surface
    and a slower build for no benefit. It previously installed the DEV
    requirements file, which pulled all three in.
    """
    assert "requirements-dev.txt" not in DOCKERFILE_DIRECTIVES
    assert "backend/requirements.txt" not in DOCKERFILE_DIRECTIVES
    assert "automation/scheduler/requirements.txt" in DOCKERFILE_DIRECTIVES
    for absent in ("Django", "djangorestframework", "django-cors-headers",
                   "pytest"):
        assert absent not in REQUIREMENTS_DIRECTIVES, absent


def test_the_timezone_database_is_an_explicit_dependency():
    """
    The entire schedule is Africa/Cairo. `ZoneInfo` falls back to the `tzdata`
    package when the base image has no system zoneinfo database, so relying on
    the base image shipping one is how a nightly job silently moves by hours.
    """
    assert "tzdata" in REQUIREMENTS_DIRECTIVES


def test_the_database_driver_is_present():
    assert "psycopg" in REQUIREMENTS_DIRECTIVES
    assert "python-dotenv" in REQUIREMENTS_DIRECTIVES


def test_no_secret_file_is_copied_into_the_image():
    """
    Secrets reach the container at RUN time through env_file, never at BUILD
    time. A COPY of an env file would bake it into a layer for ever.
    """
    for line in DOCKERFILE.splitlines():
        if line.strip().upper().startswith(("COPY", "ADD")):
            assert ".env" not in line, line


def test_the_container_does_not_run_as_root():
    assert "USER scheduler" in DOCKERFILE
    assert "useradd" in DOCKERFILE


def test_the_image_does_not_carry_the_frontend_or_the_media_worker():
    copied = [line for line in DOCKERFILE.splitlines()
              if line.strip().upper().startswith("COPY")]
    for line in copied:
        for absent in ("frontend", "services/", "tests"):
            assert absent not in line, line


# --- what the stack exposes -----------------------------------------------------

def test_the_scheduler_exposes_no_inbound_port():
    """
    It is a client of five systems and a server to none. A published port
    would be an internet-facing surface with no reason to exist.
    """
    assert "ports:" not in COMPOSE_DIRECTIVES
    assert "EXPOSE" not in DOCKERFILE_DIRECTIVES


# --- isolation from anything else on the deployment host -------------------------

def test_the_stack_declares_its_own_compose_project_name():
    """
    An explicit project name is what makes `docker compose down` here
    structurally incapable of touching another project on the same host.

    n8n is NOT that other project - it runs on a separate VPS and is reached
    over remote HTTPS only - but the deployment host is a shared company
    machine, so the isolation matters regardless of what else is on it.
    """
    assert "\nname: kbc-lecture-platform" in COMPOSE


def test_the_stack_declares_no_n8n_container_of_its_own():
    """
    n8n is a REMOTE dependency on a separate VPS, reached over HTTPS through
    N8N_BASE_URL. Nothing in this stack builds, runs or names an n8n service,
    and the only n8n configuration it carries is the URL and API key that
    arrive at runtime through env_file.
    """
    assert "n8n" not in COMPOSE_DIRECTIVES.lower()


# --- production runtime behaviour --------------------------------------------------

def test_the_container_restarts_itself_and_survives_a_host_reboot():
    assert "restart: unless-stopped" in COMPOSE


def test_a_stop_gives_a_running_cycle_time_to_finish():
    """
    The default 10 seconds would SIGKILL a cycle mid-write. That is safe - the
    transaction rolls back and PostgreSQL drops the session advisory lock -
    but finishing cleanly is better than aborting safely.
    """
    assert "stop_grace_period:" in COMPOSE


def test_logs_are_rotated_so_the_host_disk_cannot_fill():
    assert "json-file" in COMPOSE
    assert 'max-size: "20m"' in COMPOSE
    assert 'max-file: "5"' in COMPOSE


def test_the_container_runs_the_one_validated_entrypoint():
    """One scheduling implementation. The daemon calls the same run_cycle."""
    assert "scheduler-daemon" in DOCKERFILE
    assert "app.cli.main" in DOCKERFILE


def test_the_timezone_is_pinned_to_cairo_in_the_container_environment():
    assert "TZ: Africa/Cairo" in COMPOSE


def test_the_schedule_itself_is_not_hardcoded_in_the_compose_file():
    """
    It comes from backend/.env, so the container and the CLI cannot disagree
    about when the platform runs.
    """
    assert "SCHEDULER_" not in COMPOSE_DIRECTIVES
    assert "env_file" in COMPOSE_DIRECTIVES


@pytest.mark.parametrize("name", [
    "run-scheduler-cycle.bat", "install-windows-task.ps1"])
def test_the_windows_artefacts_still_exist_but_are_not_the_production_path(name):
    """
    Kept for the development laptop. Production is the container, and the two
    must never both be active - the cycle lock makes that safe, but it makes
    the logs a puzzle.
    """
    assert (SCHEDULER / name).exists()
    assert name not in COMPOSE_DIRECTIVES


# --- the image must be able to import what it runs ---------------------------

def test_the_scheduler_image_copies_everything_its_entrypoint_imports():
    """
    The contract tests above check what the image must NOT contain. This checks
    the opposite and more basic thing: that what it DOES contain is enough.

    Found by validating the release bundle, not by these tests. Five QA modules
    imported `automation.lecture_parts.graph_client`, and the Dockerfile copies
    only `app/`, so `python -m app.cli.main` raised ModuleNotFoundError inside
    the container. Every check here passed while the service could not start.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    copied = {line.split()[1].strip("./")
              for line in DOCKERFILE_DIRECTIVES
              if line.upper().startswith("COPY")}
    # Top-level packages the image actually ships.
    shipped = {name.split("/")[0] for name in copied}

    def module_path(dotted):
        base = root.joinpath(*dotted.split("."))
        for candidate in (base.with_suffix(".py"), base / "__init__.py"):
            if candidate.is_file():
                return candidate
        return None

    def imports_of(path):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                yield node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    yield alias.name

    # Walk the ENTRYPOINT's transitive import closure, not every file under
    # app/. `app.media` is deliberately outside it (see the isolation gate) and
    # still imports from automation/, which is correct for development code
    # that the scheduler image neither ships nor loads.
    entrypoint = "app.cli.main"
    seen, queue, offenders = set(), [entrypoint], []
    while queue:
        dotted = queue.pop()
        if dotted in seen:
            continue
        seen.add(dotted)
        path = module_path(dotted)
        if path is None:
            continue
        for imported in imports_of(path):
            top = imported.split(".")[0]
            if top == "app":
                queue.append(imported)
                continue
            if top in shipped:
                continue
            # No `__init__.py` requirement: `automation/` has none, and that is
            # precisely how `automation.lecture_parts.graph_client` resolved
            # from the repository root as a namespace package. Requiring one
            # here would make this test pass while the container still failed.
            if (root / top).is_dir():
                offenders.append(
                    f"{path.relative_to(root).as_posix()} imports {imported}")

    assert not offenders, (
        "the scheduler image does not copy these first-party packages, so its "
        f"entrypoint ({entrypoint}) cannot import inside the container:\n  "
        + "\n  ".join(sorted(set(offenders))))
    assert not any(m.startswith("app.media") for m in seen), (
        "app.media entered the scheduler entrypoint's import closure; QA Core "
        "must not depend on Phase 6 media code")
