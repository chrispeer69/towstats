"""Filesystem layout for the Towbook Job Acceptance Intelligence System.

Every other module resolves paths through this module. Nothing anywhere in the
codebase should build a path from the current working directory, because the
scheduler, the web server and the CLI can all be launched from different places.

Layout (all relative to REPO_ROOT):

    towbook-agent/            <- REPO_ROOT
      towbook_agent/          <- the importable package (this file lives in core/)
      config/                 <- CONFIG_DIR   : the YAML rule files
      raw/                    <- RAW_DIR      : archived XLSX, YYYY/MM/DD/
      data/                   <- DATA_DIR     : the SQLite database
      state/                  <- STATE_DIR    : session storage, logs, discovery dumps

REPO_ROOT can be overridden with the TOWBOOK_REPO_ROOT environment variable,
which is useful for tests and for running from an installed wheel.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

__all__ = [
    "REPO_ROOT",
    "PACKAGE_ROOT",
    "CONFIG_DIR",
    "RAW_DIR",
    "DATA_DIR",
    "STATE_DIR",
    "LOG_DIR",
    "DISCOVERY_DIR",
    "SESSION_DIR",
    "WEB_DIR",
    "TEMPLATES_DIR",
    "STATIC_DIR",
    "ENV_FILE",
    "ensure_dirs",
    "raw_dir_for",
    "raw_path_for",
    "resolve_under_root",
]

# --------------------------------------------------------------------------
# Anchors
# --------------------------------------------------------------------------

# .../towbook-agent/towbook_agent/core/paths.py
#      parents[0] = core, parents[1] = towbook_agent, parents[2] = repo root
PACKAGE_ROOT: Path = Path(__file__).resolve().parents[1]

_env_root = os.environ.get("TOWBOOK_REPO_ROOT", "").strip()
REPO_ROOT: Path = Path(_env_root).expanduser().resolve() if _env_root else PACKAGE_ROOT.parent

CONFIG_DIR: Path = REPO_ROOT / "config"
RAW_DIR: Path = REPO_ROOT / "raw"
DATA_DIR: Path = REPO_ROOT / "data"
STATE_DIR: Path = REPO_ROOT / "state"

# Sub-areas of STATE_DIR. These are additive conveniences; the five constants
# above are the ones named in the module contract.
LOG_DIR: Path = STATE_DIR / "logs"
DISCOVERY_DIR: Path = STATE_DIR / "discovery"
SESSION_DIR: Path = STATE_DIR / "session"

WEB_DIR: Path = PACKAGE_ROOT / "web"
TEMPLATES_DIR: Path = WEB_DIR / "templates"
STATIC_DIR: Path = WEB_DIR / "static"

ENV_FILE: Path = REPO_ROOT / ".env"

# Directories that are safe to create on demand. config/ is deliberately absent:
# a missing config directory is a real error, not something to paper over.
_RUNTIME_DIRS: tuple[Path, ...] = (
    RAW_DIR,
    DATA_DIR,
    STATE_DIR,
    LOG_DIR,
    DISCOVERY_DIR,
    SESSION_DIR,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def ensure_dirs() -> None:
    """Create every runtime directory. Idempotent and safe to call repeatedly."""
    for directory in _RUNTIME_DIRS:
        directory.mkdir(parents=True, exist_ok=True)


def resolve_under_root(value: str | Path) -> Path:
    """Resolve ``value`` against REPO_ROOT unless it is already absolute.

    Used for anything that arrives from configuration or the environment (for
    example the SQLite path inside DATABASE_URL) so that a relative path always
    means "relative to the repo", never "relative to whatever the cwd happens
    to be".
    """
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def raw_dir_for(moment: datetime) -> Path:
    """Return the archive directory for ``moment`` -> raw/YYYY/MM/DD/.

    The directory is created if it does not exist.
    """
    directory = RAW_DIR / f"{moment.year:04d}" / f"{moment.month:02d}" / f"{moment.day:02d}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def raw_path_for(moment: datetime, filename: str) -> Path:
    """Return the full archive path for ``filename`` downloaded at ``moment``."""
    return raw_dir_for(moment) / filename
