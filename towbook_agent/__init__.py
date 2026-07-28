"""Towbook Job Acceptance Intelligence System.

Watches the Towbook Digital Requests log, records every job offer, classifies
it against rules that live in YAML, computes acceptance metrics, and sends the
hourly / daily / weekly reports that a human used to text by hand.

Importing this package loads ``.env`` from the repo root (without overriding
anything already set in the real environment) so that every entrypoint -- the
CLI, the scheduler, the web app, the test suite -- sees the same credentials.
Values are read from the environment only; nothing is ever read from source.
"""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.1.0"

__all__ = ["__version__", "load_env"]

# .../towbook-agent/towbook_agent/__init__.py -> parents[1] is the repo root.
# Computed inline rather than imported from core.paths to keep package import
# free of side effects and circularity -- but it MUST honour TOWBOOK_REPO_ROOT
# with exactly the same precedence core.paths uses. Two disagreeing definitions
# of "the repo root" meant a sandboxed run (the test suite, a second
# deployment) took config/, data/ and state/ from the sandbox while still
# loading the operator's real .env from the checkout.
def _repo_root() -> Path:
    override = os.environ.get("TOWBOOK_REPO_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


_REPO_ROOT = _repo_root()
_ENV_FILE = _REPO_ROOT / ".env"


def load_env(path: Path | None = None, override: bool = False) -> bool:
    """Load environment variables from a .env file.

    Uses python-dotenv when it is installed and falls back to a tiny parser so
    that the system still starts on a machine where the dependency is missing.
    Returns True if a file was found and read.

    Existing environment variables win by default: a value exported in the
    shell or injected by the host must beat whatever is on disk.
    """
    # Recomputed rather than read from _ENV_FILE: main() calls load_env() again
    # after argument parsing, by which point TOWBOOK_REPO_ROOT may have been set.
    env_file = path or (_repo_root() / ".env")
    if not env_file.is_file():
        return False

    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except ImportError:
        _load_env_fallback(env_file, override=override)
        return True

    load_dotenv(env_file, override=override)
    return True


def _load_env_fallback(env_file: Path, override: bool = False) -> None:
    """Minimal KEY=VALUE reader used when python-dotenv is unavailable."""
    try:
        content = env_file.read_text(encoding="utf-8")
    except OSError:
        return

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value


# Load once at import. Credentials never appear in source; this only moves them
# from the operator's .env file into the process environment.
load_env()
