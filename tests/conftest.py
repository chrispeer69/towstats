"""Shared test scaffolding.

Everything here exists to make the suite obey three rules:

1. **No network, ever.** :func:`no_network` patches the socket layer so an
   accidental Twilio / SMTP / Playwright / Anthropic call fails loudly instead
   of leaving the machine. No test needs credentials.
2. **No contact with the real repo.** ``TOWBOOK_REPO_ROOT`` is pointed at a
   throwaway sandbox *before* ``towbook_agent.core.paths`` is imported, so
   ``config/``, ``data/``, ``raw/`` and ``state/`` all live under a temp
   directory. The shipped YAML configs are copied in fresh for every test, so a
   test may rewrite ``rules.yaml`` freely -- that is how hot reload gets tested.
3. **No shared state between tests.** Each test gets its own SQLite file, its
   own config copy, and a clean environment.

The sandbox is created at import time rather than in a fixture because
``core/paths.py`` resolves ``REPO_ROOT`` once, at import. Conftest is imported
before any test module, which is the only hook early enough to matter.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import socket
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import pytest

# --------------------------------------------------------------------------
# 1. Sandbox the repo root BEFORE the package is imported.
# --------------------------------------------------------------------------

TESTS_DIR = Path(__file__).resolve().parent
REAL_REPO_ROOT = TESTS_DIR.parent
SHIPPED_CONFIG_DIR = REAL_REPO_ROOT / "config"

# `import fixture_generator` from a test module.
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

SANDBOX_ROOT = Path(tempfile.mkdtemp(prefix="towbook-tests-"))
os.environ["TOWBOOK_REPO_ROOT"] = str(SANDBOX_ROOT)
for _sub in ("config", "data", "raw", "state"):
    (SANDBOX_ROOT / _sub).mkdir(parents=True, exist_ok=True)

# Never let a real credential from the operator's .env reach a test. The
# package loads .env at import; these are overwritten again per test by
# `clean_env`, but they have to be wrong before anything else runs.
_NEUTRALISED_AT_IMPORT = (
    "TOWBOOK_USER",
    "TOWBOOK_PASS",
    "TOWBOOK_ACCOUNTS",
    "ANTHROPIC_API_KEY",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASS",
    "SMTP_FROM",
)

import towbook_agent  # noqa: E402  (import must follow the env setup above)

for _key in _NEUTRALISED_AT_IMPORT:
    os.environ.pop(_key, None)

from towbook_agent.core import companies as companies_module  # noqa: E402
from towbook_agent.core import db as db_module  # noqa: E402
from towbook_agent.core import events as events_module  # noqa: E402
from towbook_agent.core import paths as paths_module  # noqa: E402
from towbook_agent.core.config_loader import CONFIG  # noqa: E402
from towbook_agent.core.models import (  # noqa: E402
    AlertFired,
    Base,
    ClientDaily,
    MetricsDaily,
    MetricsHourly,
    MetricsWeekly,
    Request,
    Run,
)

PACKAGE_DIR = Path(towbook_agent.__file__).resolve().parent
AGENTS_DIR = PACKAGE_DIR / "agents"

#: The config files copied into the sandbox for every test.
#:
#: ``companies.yaml`` is DELIBERATELY ABSENT. With no roster the system runs as
#: a single company whose id is ``"default"``, which is the shipped
#: single-company behaviour and the state every other test in the suite assumes
#: -- copying the real roster in would give every test the owner's
#: America/Detroit timezone instead of the UTC the arithmetic is hand-checked
#: against. tests/test_companies.py writes its own two-company roster with the
#: ``write_config`` fixture and reloads the registry around it.
CONFIG_FILES = (
    "rules.yaml",
    "schema.yaml",
    "selectors.yaml",
    "notifications.yaml",
    "schedule.yaml",
    "rules.proposed.yaml",
)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Delete the sandbox. Best effort -- a locked SQLite file is not a failure."""
    shutil.rmtree(SANDBOX_ROOT, ignore_errors=True)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "needs_agent(name): test requires towbook_agent/agents/<name>.py"
    )


# --------------------------------------------------------------------------
# 2. Hard offline guarantee
# --------------------------------------------------------------------------


class NetworkAccessAttempted(RuntimeError):
    """Raised when a test tries to open a network connection."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any outbound connection raise.

    SQLite is a file, so nothing legitimate in this suite needs a socket. If a
    notifier, an LLM client or a browser driver ever slipped through the other
    guards, this turns a silent real-world side effect into a red test.
    """

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise NetworkAccessAttempted(
            "a test tried to open a network connection; the suite is offline by design"
        )

    monkeypatch.setattr(socket.socket, "connect", blocked, raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked, raising=False)
    monkeypatch.setattr(socket, "create_connection", blocked, raising=False)


# --------------------------------------------------------------------------
# 3. Environment isolation
# --------------------------------------------------------------------------

#: Every environment variable the system reads. Cleared, then set to a safe
#: test value where the code needs *something* to be present.
_ENV_KEYS = (
    "TOWBOOK_USER",
    "TOWBOOK_PASS",
    "TOWBOOK_BASE_URL",
    "TOWBOOK_ACCOUNTS",
    "TZ",
    "DATABASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASS",
    "SMTP_FROM",
    "OWNER_PHONE",
    "OWNER_EMAIL",
    "OPS_MANAGER_EMAIL",
    "OPS_MANAGER_PHONE",
    "HEADLESS",
    "PLAYWRIGHT_SLOWMO",
    "LOG_LEVEL",
    "DASHBOARD_HOST",
    "DASHBOARD_PORT",
    # The dashboard password gate. Cleared like everything else so a test never
    # inherits the operator's real password from .env, and so the suite proves
    # the documented out-of-the-box default actually works.
    "DASHBOARD_PASSWORD",
    "DASHBOARD_SESSION_DAYS",
    "SESSION_SECRET",
    "SQL_ECHO",
)

#: Values every test starts with.
#:
#: TZ is UTC on purpose. The pipeline converts local -> UTC on ingest and back
#: for windowing; with TZ=UTC the two coincide, so a hand-checked number stays
#: hand-checkable. test_ingestion covers the offset conversion explicitly by
#: setting TZ to America/Detroit for one test.
_ENV_DEFAULTS = {
    "TZ": "UTC",
    "TOWBOOK_USER": "test-user@example.invalid",
    "TOWBOOK_PASS": "not-a-real-password",
    "TOWBOOK_BASE_URL": "https://app.towbook.com",
    "OWNER_PHONE": "+15550000001",
    "OWNER_EMAIL": "owner@example.invalid",
    "OPS_MANAGER_PHONE": "+15550000002",
    "OPS_MANAGER_EMAIL": "ops@example.invalid",
    "HEADLESS": "true",
    "LOG_LEVEL": "WARNING",
    # A fixed signing key, so a token minted in one test is still valid in the
    # next. DASHBOARD_PASSWORD is deliberately NOT set: leaving it unset is what
    # exercises the shipped default, which is the state a fresh deployment is in.
    "SESSION_SECRET": "test-session-secret-not-a-real-one",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Reset every environment variable the system reads.

    Credentials for Towbook, Anthropic, Twilio and SMTP are *removed*, not
    faked, so a component that needs them cannot quietly succeed against the
    real world. A test that wants a configured transport opts in explicitly.
    """
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in _ENV_DEFAULTS.items():
        monkeypatch.setenv(key, value)

    monkeypatch.setenv("TOWBOOK_REPO_ROOT", str(SANDBOX_ROOT))

    database = tmp_path / "towbook-test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
    return database


# --------------------------------------------------------------------------
# 4. Config sandbox
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def config_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Give the test a private, writable copy of the shipped YAML configs.

    The copy lives at ``<sandbox>/config`` -- the same place ``core.paths``
    resolves ``CONFIG_DIR`` to -- so code that writes a config-relative file
    (``schema.detected.yaml``) lands somewhere the test can find it.
    """
    target = SANDBOX_ROOT / "config"
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    for name in CONFIG_FILES:
        source = SHIPPED_CONFIG_DIR / name
        if source.is_file():
            shutil.copy2(source, target / name)

    # The sandbox copy declares its source timezone as UTC while TZ is also
    # UTC, so local time and stored UTC coincide and a hand-checked number
    # stays hand-checkable no matter which of the two the ingester consults.
    # The America/Detroit conversion is not skipped by this -- test_ingestion
    # sets both knobs back and asserts the offset explicitly.
    set_schema_source_timezone("UTC", config_root=target)

    monkeypatch.setattr(CONFIG, "config_dir", target, raising=False)
    CONFIG.reload()
    # The tenant registry caches the parsed roster behind the config loader's
    # own cache, so it has to be dropped too -- otherwise a test that wrote a
    # two-company companies.yaml would leave the next test resolving companies
    # that no longer exist on disk.
    companies_module.reload_companies()
    try:
        yield target
    finally:
        CONFIG.reload()
        companies_module.reload_companies()


def set_schema_scalar(key: str, value: Any, config_root: Path | None = None) -> Path:
    """Rewrite one top-level scalar in the sandbox schema.yaml, comments intact.

    A line edit rather than a YAML round-trip, because the file's comments are
    most of its value to an operator and a test should not quietly delete them.
    """
    path = (config_root or (SANDBOX_ROOT / "config")) / "schema.yaml"
    lines = path.read_text(encoding="utf-8").splitlines()
    prefix = f"{key}:"
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{key}: {value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}: {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    future = datetime.now().timestamp() + 5
    os.utime(path, (future, future))
    CONFIG.reload("schema")
    return path


def set_schema_source_timezone(timezone_name: str, config_root: Path | None = None) -> Path:
    """Point the sandbox schema at a timezone. See :func:`set_schema_scalar`."""
    return set_schema_scalar("source_timezone", timezone_name, config_root)


@pytest.fixture
def write_config(config_dir: Path) -> Callable[[str, Any], Path]:
    """Rewrite a config file and guarantee the loader notices.

    The loader keys its cache on ``(st_mtime_ns, st_size)``. A same-size edit
    inside the filesystem's timestamp granularity would be invisible, so the
    mtime is pushed forward explicitly. That is a test artefact, not a
    workaround for a bug: a human editing the file cannot hit this window.
    """
    import yaml

    def _write(name: str, data: Any) -> Path:
        filename = name if name.endswith((".yaml", ".yml")) else f"{name}.yaml"
        path = config_dir / filename
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_text(
                yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
                encoding="utf-8",
            )
        future = datetime.now().timestamp() + 5
        os.utime(path, (future, future))
        return path

    return _write


# --------------------------------------------------------------------------
# 5. Database
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_database(clean_env: Path) -> Iterator[Path]:
    """A brand new SQLite file per test, with every table created."""
    db_module.dispose_engine()
    db_module.init_db()
    try:
        yield clean_env
    finally:
        db_module.dispose_engine()


@pytest.fixture
def session() -> Iterator[Any]:
    """An open session that commits on exit."""
    with db_module.get_session() as active:
        yield active


@pytest.fixture
def read_session() -> Iterator[Any]:
    """A read-only session -- nothing is committed when it closes."""
    with db_module.get_session(commit=False) as active:
        yield active


# --------------------------------------------------------------------------
# 6. Event capture
# --------------------------------------------------------------------------


class EventCapture:
    """Records everything :func:`core.events.emit_event` dispatches."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, dict(payload)))

    # -- queries -------------------------------------------------------------

    def of_type(self, event_type: str) -> list[dict]:
        return [payload for kind, payload in self.events if kind == event_type]

    @property
    def pipeline_failures(self) -> list[dict]:
        return self.of_type(events_module.PIPELINE_FAILURE)

    @property
    def alerts(self) -> list[dict]:
        return self.of_type(events_module.ALERT)

    def alert_ids(self) -> list[str]:
        return [payload.get("alert_id", "") for payload in self.alerts]

    def __len__(self) -> int:
        return len(self.events)


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[EventCapture]:
    """Capture emitted events without letting them reach the notifier.

    ``emit_event`` always calls the notifier sink first. That sink is replaced
    here, because an event escaping into a half-configured notifier during an
    ingestion test is noise, not signal. test_notifier exercises the real sink.
    """
    capture = EventCapture()
    monkeypatch.setattr(events_module, "_notifier_sink", capture, raising=True)
    events_module.register_handler(capture)
    try:
        yield capture
    finally:
        events_module.unregister_handler(capture)


@pytest.fixture(autouse=True)
def clean_event_log() -> Iterator[None]:
    """Start each test with an empty state/events.jsonl."""
    log = events_module.STATE_DIR / "events.jsonl" if hasattr(events_module, "STATE_DIR") else None
    log = log or (SANDBOX_ROOT / "state" / "events.jsonl")
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists():
        log.unlink()
    yield
    events_module.clear_handlers()


def read_event_log() -> list[dict[str, Any]]:
    """Return the durable event trace written by core.events."""
    log = SANDBOX_ROOT / "state" / "events.jsonl"
    if not log.is_file():
        return []
    lines = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


# --------------------------------------------------------------------------
# 7. Loading the agent modules
# --------------------------------------------------------------------------


def load_agent(name: str):
    """Import ``towbook_agent.agents.<name>``.

    Skips -- loudly and specifically -- when the module has not been written
    yet, so the foundation tests still run during a staged build. If the file
    exists, any import error is a real failure and is allowed to propagate.
    """
    module_path = AGENTS_DIR / f"{name}.py"
    if not module_path.is_file():
        pytest.skip(f"agents/{name}.py has not been written yet ({module_path})")
    return importlib.import_module(f"towbook_agent.agents.{name}")


@pytest.fixture
def classifier():
    return load_agent("classifier")


@pytest.fixture
def ingestion():
    return load_agent("ingestion")


@pytest.fixture
def metrics():
    return load_agent("metrics")


@pytest.fixture
def notifier():
    return load_agent("notifier")


# --------------------------------------------------------------------------
# 8. Reading values out of loosely specified return dicts
# --------------------------------------------------------------------------

_MISSING = object()

#: Names that mean the same thing. The module contract fixes the metrics
#: function *signatures*, not the key names in the dicts they return, so a test
#: that asks for "rate" will accept "acceptance_rate" -- and still fails loudly
#: if the number itself is absent or wrong.
_ALIASES: dict[str, tuple[str, ...]] = {
    "rate": ("rate", "acceptance_rate", "acceptance_rate_window"),
    "offered": ("offered", "offers", "offered_count"),
    "accepted": ("accepted", "accepted_count"),
}


def dig(data: Any, *names: str, default: Any = _MISSING) -> Any:
    """Fetch the first of ``names`` from ``data``, looking one level down too.

    The module contract fixes the *signature* of the metrics functions but not
    the shape of the dict they return, so a test asserts on the value and
    tolerates whether it sits at the top level or inside a ``totals`` /
    ``summary`` / ``metrics`` sub-dict. Anything the contract does pin down --
    the database columns -- is asserted exactly, not through here.
    """
    if not isinstance(data, dict):
        if default is _MISSING:
            raise AssertionError(f"expected a dict, got {type(data).__name__}: {data!r}")
        return default

    wanted: list[str] = []
    for name in names:
        for alias in _ALIASES.get(name, (name,)):
            if alias not in wanted:
                wanted.append(alias)

    for name in wanted:
        if name in data:
            return data[name]

    for container in ("totals", "summary", "metrics", "overall", "counts"):
        nested = data.get(container)
        if isinstance(nested, dict):
            for name in wanted:
                if name in nested:
                    return nested[name]

    if default is _MISSING:
        raise AssertionError(
            f"none of {tuple(wanted)} found in metrics result; keys were {sorted(data)}"
        )
    return default


def attr(obj: Any, *names: str, default: Any = _MISSING) -> Any:
    """Same idea as :func:`dig`, for the loosely specified ``IngestResult``."""
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    if default is _MISSING:
        raise AssertionError(f"none of {names} found on {type(obj).__name__}: {obj!r}")
    return default


# --------------------------------------------------------------------------
# 9. Small helpers used by several test modules
# --------------------------------------------------------------------------


#: Source status strings that mean the offer became a job, and therefore that
#: Towbook issued a call number for it. Only the ones :func:`make_row` is
#: called with -- config/schema.yaml holds the full vocabulary.
_ACCEPTED_STATUS_STRINGS: frozenset[str] = frozenset(
    {"accepted", "accept", "accept sent", "completed", "complete", "dispatched"}
)


def make_row(
    request_id: str,
    *,
    client: str = "Agero",
    offered_at: datetime | None = None,
    status: str = "Accepted",
    service: str = "Light Duty Tow",
    denial_reason: str = "",
    amount: float | None = None,
    responded_at: datetime | str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build one canonical-field row dict for ``write_rows_xlsx``.

    Times are local (TZ), because that is what the portal exports.
    """
    offered_at = offered_at or datetime(2026, 7, 20, 14, 0, 0)
    if responded_at is None:
        responded_at = (offered_at + timedelta(minutes=2)).strftime("%m/%d/%Y %I:%M:%S %p")
    elif isinstance(responded_at, datetime):
        responded_at = responded_at.strftime("%m/%d/%Y %I:%M:%S %p")

    row: dict[str, Any] = {
        "request_id": request_id,
        "client_name": client,
        "offered_at": offered_at,
        "status": status,
        "service_type_raw": service,
        "responded_at": responded_at,
        "denial_reason": denial_reason,
        "pickup_location": "I-75 NB @ Mile 42, Flint MI",
        "dropoff_location": "Owner Residence",
        "truck_assigned": "Unit 7",
        "driver_assigned": "M. Alvarez",
        "amount": amount,
        # The Towbook call number, which maps to `job_number`. Present only on
        # an accepted row, because that is when Towbook issues one -- an offer
        # that expired or was rejected never became a job and never got a
        # number. Pass `**{"Call Number": ...}` to override.
        #
        # Derived from request_id with a stable arithmetic, NOT hash(): str
        # hashing is salted per process, so that version handed the same row a
        # different job number on every run.
        "Call Number": (
            f"C{100000 + sum(ord(char) for char in request_id) % 900000}"
            if str(status).strip().casefold() in _ACCEPTED_STATUS_STRINGS
            else ""
        ),
        "PO#": "",
        # One car per offer. A constant here would make every row in a test
        # export the same customer's job to `duplicate_offers`, and a two-row
        # fixture would silently become a one-row report. Pass
        # `**{"Vehicle": ...}` to make two rows deliberately the same car.
        "Vehicle": f"2019 Ford F-150 #{request_id}",
        "Request Expiration Time": (offered_at + timedelta(minutes=8)).strftime(
            "%m/%d/%Y %I:%M:%S %p"
        ),
    }
    row.update(extra)
    return row


def count_rows(model: Any) -> int:
    """Row count for a model, using its own session."""
    from sqlalchemy import func, select

    with db_module.get_session(commit=False) as active:
        return int(active.execute(select(func.count()).select_from(model)).scalar_one())


def all_requests() -> list[Request]:
    from sqlalchemy import select

    with db_module.get_session(commit=False) as active:
        return list(active.execute(select(Request).order_by(Request.request_id)).scalars())


def get_request(request_id: str) -> Request | None:
    with db_module.get_session(commit=False) as active:
        return active.get(Request, request_id)


def ingest_file(ingestion_module: Any, path: Path, run_id: str = "test-run") -> Any:
    """Call ``ingest`` and return ``(result, error)`` without letting it explode.

    Ingestion may signal a hard failure either by raising or by returning a
    result that says it failed; the spec does not choose. Tests assert on the
    observable consequences -- rows written, files written, events emitted --
    which are the same either way.
    """
    try:
        return ingestion_module.ingest(path, run_id), None
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        return None, exc


__all__ = [
    "SANDBOX_ROOT",
    "REAL_REPO_ROOT",
    "SHIPPED_CONFIG_DIR",
    "AGENTS_DIR",
    "EventCapture",
    "NetworkAccessAttempted",
    "load_agent",
    "set_schema_scalar",
    "set_schema_source_timezone",
    "dig",
    "attr",
    "make_row",
    "count_rows",
    "all_requests",
    "get_request",
    "ingest_file",
    "read_event_log",
    "Base",
    "Request",
    "Run",
    "MetricsHourly",
    "MetricsDaily",
    "MetricsWeekly",
    "ClientDaily",
    "AlertFired",
    "date",
    "datetime",
    "timedelta",
    "Sequence",
]
