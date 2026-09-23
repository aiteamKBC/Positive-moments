"""
The RC release gate: one command, everything that must pass before a tag.

    APP_ENV=test TEST_DATABASE_URL=postgresql://.../kbc_qa_integration_test \
        python -m tools.release_gate [--reset]

It runs, in one pytest session:

  * the offline unit suite (tests/unit) and automation/lecture_parts;
  * the self-contained integration suite (tests/integration, `-m "not
    production_data"`), which covers the migrations, the scheduler and
    orchestration, and the F-01/F-02/F-03 safety contracts against real
    PostgreSQL;

and deselects the production-data acceptance suite, which needs an approved
acceptance dataset and is never pointed at production.

`--reset` rebuilds the isolated test database from the repository's migrations
first, so the gate is proven on a database built from nothing.

The gate PASSES only when nothing failed. Deselected and skipped counts are
reported, never hidden.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

from tools.integration_db_guard import REPO_ROOT, UnsafeTestDatabase, check_static


GATE_ARGS = ["-m", "not production_data", "-p", "no:randomly"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--reset", action="store_true",
                        help="rebuild the isolated test database from the migrations first")
    # Unknown arguments pass straight through to pytest (-q, --junitxml, -k ...).
    args, pytest_args = parser.parse_known_args(argv)

    try:
        target = check_static()
    except UnsafeTestDatabase as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(f"release gate database: {target.describe()}")

    if args.reset:
        bootstrap = subprocess.run(
            [sys.executable, "-m", "tools.bootstrap_integration_db", "--reset"],
            cwd=REPO_ROOT)
        if bootstrap.returncode != 0:
            return bootstrap.returncode

    result = subprocess.run([sys.executable, "-m", "pytest", *GATE_ARGS,
                             *pytest_args], cwd=REPO_ROOT)
    print("\nRC RELEASE TEST GATE:", "PASS" if result.returncode == 0 else "FAIL")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
