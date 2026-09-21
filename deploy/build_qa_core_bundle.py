"""
Build the QA Core release bundle from the validated working tree.

WHY NOT `git archive`
---------------------
Because `git clone` is NOT currently sufficient: `app/`, `backend/operations/`,
`automation/`, `services/`, `tests/` and `docs/` are all untracked in this
repository. A clone would produce a release with no application in it. The
bundle is therefore built from the working tree's dependency closure, by an
explicit allow-list, and every exclusion is stated rather than assumed.

WHAT IS DELIBERATELY NOT COPIED
-------------------------------
Secrets of any kind (`.env`, tokens, credentials), virtual environments,
`node_modules`, caches, build output, local media (`output/`), diagnostic
clips, temporary recordings, and the media worker service.

Re-runnable: the destination is emptied first, so a second run produces the
same bundle rather than a merge of two.
"""
import hashlib
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Bumped per release candidate. rc2 adds Operations Backfill.
RELEASE = "qa-core-rc2"
BUNDLE = ROOT / "release" / RELEASE

# Directory trees the release needs, copied whole minus the excluded names.
TREES = [
    "app",                      # the platform package (incl. db/migrations)
    "backend",                  # Django project: config, operations, positive_mentions
    "frontend",                 # the console source; built inside its image
    "automation/scheduler",     # scheduler Dockerfile, requirements, compose
    "deploy",                   # QA Core compose, Dockerfiles, env template
    "tests",                    # so the infrastructure team can re-verify
]

FILES = [
    "QA_CORE_RELEASE_READINESS.md",
    "DEPLOYMENT_HANDOFF.md",
    "pytest.ini",
    "requirements-dev.txt",
]

# Never copied, at any depth. Ordered by why they are excluded.
EXCLUDED_NAMES = {
    # secrets
    ".env", ".env.local", ".env.production", "backend.env", "secrets.json",
    # environments and dependencies
    ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".gitignore-local",
    # build and local output
    "dist", "build", ".vite", "data", "output", "tmp",
    # version control and editor
    ".git", ".idea", ".vscode",
}

# Media development that is NOT part of this release. Excluded by path, not by
# name, because `automation/` also holds the scheduler, which IS part of it.
EXCLUDED_PATHS = {
    "services/kbc-media-worker",
    "automation/lecture_parts",
    "automation/positive_clips",
}

# A file with any of these extensions is never packaged, wherever it is.
EXCLUDED_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".m4a", ".wav", ".pyc",
                     ".log", ".sqlite3", ".pem", ".key", ".pfx"}

# Content that would mean a secret slipped through.
#
# An earlier version of this scan matched the bare word, which flagged every
# Python keyword argument (`api_key=settings.qa_model_api_key`) and was
# therefore noise - and a noisy audit is one nobody reads. These patterns match
# an assignment to a LITERAL only: a quoted string or a bare token, never an
# expression, an attribute lookup, a call or a placeholder.
SECRET_PATTERNS = (
    ("credential assignment", re.compile(
        r"""(?ix)
        \b (?: client_secret | api_key | secret_key | password | token )
        \s* [=:] \s*
        (?: ["'] (?P<quoted> [^"'\n]{12,} ) ["'] | (?P<bare> [A-Za-z0-9_\-]{20,} ) )
        """)),
    ("database URL with credentials", re.compile(
        r"(?i)postgres(?:ql)?://[^\s:/]+:[^\s@]+@")),
    ("bearer token", re.compile(r"Bearer\s+ey[A-Za-z0-9._\-]{20,}")),
    ("signed URL", re.compile(r"[?&](?:sig|sv|se)=[A-Za-z0-9%+/=]{16,}")),
)

# A literal that is obviously not a live credential.
SAFE_LITERALS = re.compile(
    r"""(?ix) ^ (?:
        \s* $ | os\. | settings\. | self\. | config\[ | env\[ |      # expressions
        [<{$%] |                                                     # placeholders
        (?: correct-horse | changeme | example | placeholder |
            your- | xxx | test- | dummy | fake |
            # Test fixtures that name themselves as fixtures. A real OpenAI key
            # is `sk-` plus ~48 opaque characters; these read as English.
            sk-secret-value | secret-value | not-a-real )
    )""")

# Files that legitimately contain these words as NAMES or documentation.
MARKER_ALLOWLIST = {".md", ".template", ".example", ".txt"}


def excluded(path: Path) -> bool:
    relative = path.relative_to(ROOT).as_posix()
    if any(relative == p or relative.startswith(p + "/") for p in EXCLUDED_PATHS):
        return True
    if any(part in EXCLUDED_NAMES for part in path.parts):
        return True
    return path.is_file() and path.suffix.lower() in EXCLUDED_SUFFIXES


def copy_tree(relative: str, copied: list) -> None:
    source = ROOT / relative
    if not source.exists():
        print(f"  MISSING  {relative}")
        return
    for item in sorted(source.rglob("*")):
        if item.is_dir() or excluded(item):
            continue
        destination = BUNDLE / item.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
        copied.append(destination)


def scan_for_secrets(files: list) -> list:
    findings = []
    for path in files:
        if path.suffix.lower() in MARKER_ALLOWLIST:
            continue
        if path.name == Path(__file__).name:        # this scanner's own patterns
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for label, pattern in SECRET_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                groups = match.groupdict() if match.groupdict() else {}
                value = groups.get("quoted") or groups.get("bare") or match.group(0)
                if SAFE_LITERALS.match(value):
                    continue
                findings.append((
                    f"{path.relative_to(BUNDLE).as_posix()}:{line_number}",
                    label, line.strip()[:90]))
    return findings


def main() -> int:
    if BUNDLE.exists():
        shutil.rmtree(BUNDLE)
    BUNDLE.mkdir(parents=True)

    copied = []
    print(f"building {BUNDLE.relative_to(ROOT).as_posix()}\n")
    for tree in TREES:
        before = len(copied)
        copy_tree(tree, copied)
        print(f"  {tree:<24} {len(copied) - before:>5} files")
    for name in FILES:
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, BUNDLE / name)
            copied.append(BUNDLE / name)
            print(f"  {name:<24} {'1':>5} file")
        else:
            print(f"  MISSING  {name}")

    total_bytes = sum(p.stat().st_size for p in copied)
    print(f"\n  {len(copied)} files, {total_bytes / 1_048_576:.1f} MiB")

    print("\nEXCLUSION AUDIT")
    for label, found in (
            (".env / secrets", list(BUNDLE.rglob(".env"))),
            ("node_modules", list(BUNDLE.rglob("node_modules"))),
            ("virtualenvs", list(BUNDLE.rglob(".venv"))),
            ("media files", [p for p in BUNDLE.rglob("*")
                             if p.suffix.lower() in {".mp4", ".mkv", ".mov"}]),
            ("media worker", list(BUNDLE.rglob("kbc-media-worker"))),
            ("build output", list(BUNDLE.rglob("dist"))),
            ("caches", list(BUNDLE.rglob("__pycache__")))):
        print(f"  [{'FAIL' if found else ' OK '}] no {label}"
              f"{'  ' + str(len(found)) + ' found' if found else ''}")

    findings = scan_for_secrets(copied)
    print(f"  [{'FAIL' if findings else ' OK '}] no secret-shaped value in any "
          f"packaged file")
    for where, marker, line in findings[:10]:
        print(f"         {where}: {marker}  {line}")

    digest = hashlib.sha256()
    for path in sorted(copied):
        digest.update(path.relative_to(BUNDLE).as_posix().encode())
        digest.update(path.read_bytes())
    print(f"\n  bundle sha256: {digest.hexdigest()}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
