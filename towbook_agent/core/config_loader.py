"""Hot-reloading loader for the YAML rule files in config/.

Hard constraint #2: rules are data, not code. Adding a service type, an
acceptance policy, an alert condition, a notification route or a schedule entry
must require zero code change and zero redeploy. "Zero redeploy" is the part
that lands here -- a long-running scheduler process has to notice that
rules.yaml changed on disk without being restarted.

The loader re-stats each file on every access and reloads when the modification
time or size changed. That is cheap (one stat syscall) and correct for the way
these files are edited: a human saves the file, the next job picks it up.

Usage:

    from towbook_agent.core.config_loader import get_rules, rules_version

    rules = get_rules()
    version = rules_version()   # e.g. "1-3f9a1c0d2b77"

``rules_version()`` combines the ``version:`` key inside rules.yaml with a
sha256 of the file bytes, so that an edit which the author forgot to bump the
version for still produces a distinct stamp. Every metrics row and every run
records it, which is what makes a recomputation traceable.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from .paths import CONFIG_DIR

__all__ = [
    "ConfigLoader",
    "ConfigError",
    "CONFIG",
    "get_rules",
    "get_schema",
    "get_selectors",
    "get_notifications",
    "get_schedule",
    "get_proposed_rules",
    "get_companies",
    "rules_version",
    "reload_all",
    "config_path",
]

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """A config file is missing, unreadable, or not a YAML mapping."""


#: Logical name -> filename. Optional files return {} when absent.
_FILES: Final[dict[str, str]] = {
    "rules": "rules.yaml",
    "schema": "schema.yaml",
    "selectors": "selectors.yaml",
    "notifications": "notifications.yaml",
    "schedule": "schedule.yaml",
    "rules_proposed": "rules.proposed.yaml",
    "companies": "companies.yaml",
}

#: Names that may legitimately be missing from disk. ``companies`` is optional
#: because a single-company install needs no roster at all: core/companies.py
#: falls back to one company whose id is the ``"default"`` that every existing
#: row already carries.
_OPTIONAL: Final[frozenset[str]] = frozenset({"rules_proposed", "companies"})


@dataclass
class _Entry:
    """One cached config file."""

    data: dict[str, Any]
    digest: str
    stamp: tuple[int, int]  # (st_mtime_ns, st_size)


class ConfigLoader:
    """Reads and caches the YAML config files, reloading them when they change.

    Thread safe. The scheduler, the web app and the CLI can share the module
    level singleton ``CONFIG``.
    """

    def __init__(self, config_dir: Path | str | None = None) -> None:
        self.config_dir: Path = Path(config_dir) if config_dir else CONFIG_DIR
        self._cache: dict[str, _Entry] = {}
        self._lock = threading.RLock()

    # -- paths ---------------------------------------------------------------

    def path_for(self, name: str) -> Path:
        """Return the path of the config file registered under ``name``."""
        try:
            filename = _FILES[name]
        except KeyError:
            raise ConfigError(
                f"unknown config {name!r}; known names: {', '.join(sorted(_FILES))}"
            ) from None
        return self.config_dir / filename

    # -- loading -------------------------------------------------------------

    @staticmethod
    def _stamp(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _read(self, name: str, path: Path) -> _Entry:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            if name in _OPTIONAL:
                return _Entry(data={}, digest="", stamp=(0, 0))
            raise ConfigError(f"config file not found: {path}") from None
        except OSError as exc:
            raise ConfigError(f"could not read config file {path}: {exc}") from exc

        try:
            parsed = yaml.safe_load(raw.decode("utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise ConfigError(f"{path.name} is not valid UTF-8: {exc}") from exc

        if parsed is None:
            parsed = {}
        if not isinstance(parsed, dict):
            raise ConfigError(
                f"{path.name} must contain a YAML mapping at the top level, "
                f"got {type(parsed).__name__}"
            )

        stamp = self._stamp(path) or (0, 0)
        return _Entry(
            data=parsed,
            digest=hashlib.sha256(raw).hexdigest(),
            stamp=stamp,
        )

    def get(self, name: str) -> dict[str, Any]:
        """Return the parsed config for ``name``, reloading it if it changed."""
        path = self.path_for(name)
        with self._lock:
            current = self._stamp(path)
            cached = self._cache.get(name)

            if cached is not None and current is not None and current == cached.stamp:
                return cached.data

            if current is None and cached is not None:
                # The file vanished mid-flight -- an editor writing atomically
                # can produce this. Keep serving the last known good copy.
                logger.warning(
                    "config file %s is temporarily unreadable, serving cached copy", path
                )
                return cached.data

            entry = self._read(name, path)
            if cached is not None and entry.digest != cached.digest:
                logger.info("reloaded %s (digest %s)", path.name, entry.digest[:12])
            self._cache[name] = entry
            return entry.data

    def digest(self, name: str) -> str:
        """Return the full sha256 hex digest of the file backing ``name``."""
        self.get(name)  # ensures the cache is fresh
        with self._lock:
            entry = self._cache.get(name)
            return entry.digest if entry else ""

    def reload(self, name: str | None = None) -> None:
        """Drop cached copies so the next access re-reads from disk."""
        with self._lock:
            if name is None:
                self._cache.clear()
            else:
                self._cache.pop(name, None)

    # -- typed accessors -----------------------------------------------------

    def get_rules(self) -> dict[str, Any]:
        return self.get("rules")

    def get_schema(self) -> dict[str, Any]:
        return self.get("schema")

    def get_selectors(self) -> dict[str, Any]:
        return self.get("selectors")

    def get_notifications(self) -> dict[str, Any]:
        return self.get("notifications")

    def get_schedule(self) -> dict[str, Any]:
        return self.get("schedule")

    def get_proposed_rules(self) -> dict[str, Any]:
        """The Analyst's suggested rule changes. Never auto-applied."""
        return self.get("rules_proposed")

    def get_companies(self) -> dict[str, Any]:
        """The tenant roster. ``{}`` on a single-company install.

        Parsed into :class:`~towbook_agent.core.companies.Company` records by
        core/companies.py; this only reads the file.
        """
        return self.get("companies")

    # -- versioning ----------------------------------------------------------

    def rules_version(self) -> str:
        """Return ``"<version key>-<first 12 hex of sha256(rules.yaml)>"``."""
        rules = self.get_rules()
        version = rules.get("version", "0")
        digest = self.digest("rules")[:12]
        return f"{version}-{digest}" if digest else str(version)

    def snapshot(self) -> dict[str, str]:
        """Return ``{config name: short digest}`` for every loaded file.

        Useful in diagnostics and on the dashboard: it answers "which rules
        produced this number?" without opening the files.
        """
        result: dict[str, str] = {}
        for name in _FILES:
            try:
                result[name] = self.digest(name)[:12]
            except ConfigError:
                result[name] = "missing"
        return result

    # -- selector helper -----------------------------------------------------

    def selector_candidates(self, section: str, key: str) -> list[str]:
        """Return the candidate selector list at ``selectors[section][key]``.

        Hard constraint #7: zero hardcoded selectors in Python. Every selector
        value in selectors.yaml is a *list* of candidates tried in order, so
        discovery can append new ones without a code change. A scalar in the
        YAML is tolerated and wrapped into a one-element list.
        """
        selectors = self.get_selectors()
        block = selectors.get(section)
        if not isinstance(block, dict):
            raise ConfigError(f"selectors.yaml has no section {section!r}")
        if key not in block:
            raise ConfigError(f"selectors.yaml section {section!r} has no key {key!r}")
        value = block[key]
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if item is not None]
        raise ConfigError(
            f"selectors.yaml {section}.{key} must be a list of candidate selectors, "
            f"got {type(value).__name__}"
        )


# --------------------------------------------------------------------------
# Module level singleton and the free functions named in the module contract
# --------------------------------------------------------------------------

CONFIG: Final[ConfigLoader] = ConfigLoader()


def get_rules() -> dict[str, Any]:
    return CONFIG.get_rules()


def get_schema() -> dict[str, Any]:
    return CONFIG.get_schema()


def get_selectors() -> dict[str, Any]:
    return CONFIG.get_selectors()


def get_notifications() -> dict[str, Any]:
    return CONFIG.get_notifications()


def get_schedule() -> dict[str, Any]:
    return CONFIG.get_schedule()


def get_proposed_rules() -> dict[str, Any]:
    return CONFIG.get_proposed_rules()


def get_companies() -> dict[str, Any]:
    return CONFIG.get_companies()


def rules_version() -> str:
    return CONFIG.rules_version()


def reload_all() -> None:
    CONFIG.reload()


def config_path(name: str) -> Path:
    return CONFIG.path_for(name)
