"""Everything that only breaks once the code is on a server.

This suite exists because the failure modes it covers are all *silent*. Nothing
here raises in development:

* a SQLite database on a container host works perfectly until the redeploy that
  deletes it;
* ``postgres://`` is the string Railway shows you and the one SQLAlchemy 2
  refuses;
* a ``PRAGMA`` on a PostgreSQL connection is a syntax error at connect time, so
  it happens before any query and looks like the database is down;
* a scheduler that never started leaves a board that renders yesterday's numbers
  as though they were today's;
* an SMS route left enabled in the shipped config bills whoever deploys this
  next for texts they did not ask for.

None of it needs a PostgreSQL server: the dialect, the DDL and the URL handling
are all checkable offline, and the suite is offline by design.
"""

from __future__ import annotations

import json
import re
import socket
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from conftest import REAL_REPO_ROOT
from towbook_agent.core import db as db_module
from towbook_agent.core.models import Base

#: Captured at import, BEFORE conftest's ``no_network`` fixture replaces them.
#: Grabbing these inside a fixture would capture the blocked stubs and make the
#: narrowed guard below permanently closed.
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex

# ==========================================================================
# DATABASE_URL: the two rewrites a hosted deployment cannot boot without
# ==========================================================================


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        # The legacy scheme Railway's dashboard still shows. SQLAlchemy 2 raises
        # on it outright, so pasting the provider's own value would crash.
        ("postgres://u:p@host:5432/db", "postgresql+psycopg://u:p@host:5432/db"),
        # The modern scheme. Valid, but it selects psycopg2, which is not
        # installed -- so it must be pinned to the driver that is.
        ("postgresql://u:p@host:5432/db", "postgresql+psycopg://u:p@host:5432/db"),
        # An explicit driver is somebody's decision and is left alone.
        ("postgresql+psycopg2://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
        ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        # SQLite is untouched: local development and this test suite.
        ("sqlite:///data/towbook.db", "sqlite:///data/towbook.db"),
        ("sqlite://", "sqlite://"),
        ("", ""),
    ],
)
def test_the_url_provider_hands_over_is_rewritten_into_one_sqlalchemy_accepts(
    supplied: str, expected: str
) -> None:
    assert db_module.normalize_database_url(supplied) == expected


def test_the_rewrite_survives_a_password_with_url_characters() -> None:
    """A generated Postgres password contains @ / : and they must not move."""
    url = db_module.normalize_database_url("postgres://u:p%40ss%3Aword@host:5432/rail")
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    assert parsed.drivername == "postgresql+psycopg"
    assert parsed.password == "p@ss:word"
    assert parsed.host == "host"
    assert parsed.database == "rail"


def test_database_url_applies_the_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything -- engine, alembic env.py, CLI -- reads through this one function."""
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@host/db")
    assert db_module.database_url() == "postgresql+psycopg://u:p@host/db"
    assert db_module.is_postgres()
    assert not db_module.is_sqlite()


def test_the_postgres_driver_is_importable() -> None:
    """requirements.txt must actually install the driver the URL now names."""
    engine = create_engine("postgresql+psycopg://u:p@127.0.0.1:1/db")
    assert engine.dialect.name == "postgresql"
    assert engine.dialect.driver == "psycopg"


# ==========================================================================
# The SQLite-only pragmas must not reach PostgreSQL
# ==========================================================================


def test_sqlite_pragmas_are_registered_only_for_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PRAGMA on a Postgres connection fails at connect time, before any query.

    That failure looks exactly like "the database is unreachable", which is the
    single most misleading way for a deployment to break.
    """
    from sqlalchemy import event

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/db")
    db_module.reset_engine()
    try:
        engine = db_module.get_engine()
        assert not event.contains(engine, "connect", db_module._sqlite_on_connect)
    finally:
        db_module.reset_engine()

    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    db_module.reset_engine()
    try:
        engine = db_module.get_engine()
        assert event.contains(engine, "connect", db_module._sqlite_on_connect)
    finally:
        db_module.reset_engine()


def test_postgres_gets_a_bounded_pool_and_sqlite_gets_thread_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One web process now serves AND schedules, against a capped connection limit."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/db")
    db_module.reset_engine()
    try:
        pool = db_module.get_engine().pool
        assert pool.size() == 5
        assert pool._recycle == 1800
    finally:
        db_module.reset_engine()

    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    db_module.reset_engine()
    try:
        # The scheduler writes from a worker thread while the app reads.
        assert db_module.get_engine().pool._dialect.dbapi is not None
    finally:
        db_module.reset_engine()


# ==========================================================================
# The schema itself has to be portable
# ==========================================================================


def test_every_table_compiles_to_valid_postgresql_ddl() -> None:
    """No SQLite-ism in any column type, default or constraint.

    Rendering the DDL is the whole check: SQLAlchemy raises
    ``CompileError`` for a type or construct the dialect cannot express, and a
    silent difference (JSON, SERIAL, TIMESTAMP WITHOUT TIME ZONE) is exactly
    what portable column types are for.
    """
    dialect = postgresql.dialect()
    for name, table in sorted(Base.metadata.tables.items()):
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert f"CREATE TABLE {name}" in ddl
        assert "PRAGMA" not in ddl
        # SQLite spells an autoincrement key AUTOINCREMENT; Postgres SERIAL.
        assert "AUTOINCREMENT" not in ddl.upper()


def test_the_json_columns_are_json_on_both_backends() -> None:
    """metrics blobs are JSON, not TEXT-with-a-convention."""
    from sqlalchemy.dialects import sqlite as sqlite_dialect

    for table_name in ("metrics_daily", "metrics_weekly", "metrics_monthly"):
        column = Base.metadata.tables[table_name].c.metrics
        assert "JSON" in str(column.type.compile(dialect=postgresql.dialect())).upper()
        assert "JSON" in str(column.type.compile(dialect=sqlite_dialect.dialect())).upper()


def test_the_upsert_picks_a_dialect_specific_insert() -> None:
    """ON CONFLICT is spelled by the dialect, never as raw SQLite SQL.

    ``agents.ingestion._upsert`` reads the dialect off the bound session. If it
    ever hardcoded ``sqlite.insert``, this is where it shows up -- and on
    Postgres the failure would be an exception per ingest, i.e. no data at all.
    """
    import inspect as _inspect

    from towbook_agent.agents import ingestion

    source = _inspect.getsource(ingestion._upsert)
    assert "sqlalchemy.dialects.postgresql" in source
    assert "sqlalchemy.dialects.sqlite" in source
    assert "on_conflict_do_update" in source
    # And the fallback for anything else is a merge, not a crash.
    assert "session.merge" in source


# ==========================================================================
# alembic upgrade head, in the three states a deployment actually meets
# ==========================================================================


@pytest.fixture
def repo_root_for_migrations(monkeypatch: pytest.MonkeyPatch):
    """Point paths at the real repo: the sandbox has no alembic/ directory."""
    from towbook_agent.core import paths as paths_module

    monkeypatch.setattr(paths_module, "REPO_ROOT", REAL_REPO_ROOT)
    yield
    db_module.reset_engine()


@pytest.fixture
def booted_client(monkeypatch: pytest.MonkeyPatch, no_network: None):
    """A TestClient entered as a context manager, so the ASGI lifespan runs.

    Two things this needs that a bare ``TestClient(app)`` does not:

    * the lifespan. Starlette only runs it inside ``with``, and the lifespan is
      the entire boot sequence -- migrate, then start the scheduler. A test that
      skips it tests a different program from the one Railway runs.
    * loopback. ``TestClient`` drives the app on an anyio portal, and asyncio's
      Windows proactor loop builds its self-pipe with ``socket.socketpair()``,
      which connects to 127.0.0.1. That is the process talking to itself, which
      conftest's offline guard cannot distinguish from a request leaving the
      machine. The guard is narrowed, not lifted: anything that is not loopback
      still raises.
    """
    from fastapi.testclient import TestClient

    from conftest import NetworkAccessAttempted
    from towbook_agent.web.app import app

    loopback = {"127.0.0.1", "::1", "localhost", ""}

    def guarded(original):
        def call(self, address, *args, **kwargs):
            host = address[0] if isinstance(address, (tuple, list)) and address else None
            if str(host) not in loopback:
                raise NetworkAccessAttempted(
                    f"a test tried to reach {address!r}; the suite is offline by design"
                )
            return original(self, address, *args, **kwargs)

        return call

    monkeypatch.setattr(socket.socket, "connect", guarded(_REAL_CONNECT), raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(_REAL_CONNECT_EX), raising=False)
    monkeypatch.setenv("RUN_SCHEDULER", "true")

    with TestClient(app) as client:
        yield client


def test_upgrade_to_head_builds_a_brand_new_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_root_for_migrations
) -> None:
    """State 1: the empty Postgres a container is handed on its first deploy."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}")
    db_module.reset_engine()

    result = db_module.upgrade_to_head()

    assert result["ok"], result["error"]
    tables = set(inspect(db_module.get_engine()).get_table_names())
    assert set(Base.metadata.tables) <= tables
    assert "alembic_version" in tables


def test_upgrade_to_head_is_a_no_op_the_second_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_root_for_migrations
) -> None:
    """State 2: every redeploy after the first. It runs on every boot."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'again.db').as_posix()}")
    db_module.reset_engine()

    first = db_module.upgrade_to_head()
    second = db_module.upgrade_to_head()

    assert first["ok"] and second["ok"]
    assert first["action"] == "created"
    assert first["previous_revision"] is None
    assert second["action"] == "up_to_date"
    assert second["previous_revision"] == second["revision"]
    assert first["revision"] == second["revision"]


def test_upgrade_to_head_reports_that_it_actually_upgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_root_for_migrations
) -> None:
    """The deploy that ships a migration is the one whose log is worth reading.

    ``action`` is derived from the version row before and after, not from
    "were there tables" -- which would report the interesting case as
    ``up_to_date`` and say nothing happened when something did.
    """
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    database = tmp_path / "partial.db"
    url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    db_module.reset_engine()

    config = Config(str(REAL_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REAL_REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    revisions = list(ScriptDirectory.from_config(config).walk_revisions())
    if len(revisions) < 2:
        pytest.skip("only one migration exists; there is no partial state to build")

    # Stop one revision short of the head, the state a container is in when a
    # release adds a migration.
    command.upgrade(config, revisions[1].revision)
    db_module.reset_engine()

    result = db_module.upgrade_to_head()

    assert result["ok"], result["error"]
    assert result["action"] == "upgraded"
    assert result["previous_revision"] == revisions[1].revision
    assert result["revision"] == revisions[0].revision


def test_upgrade_to_head_repairs_a_database_init_db_created_before_stamping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_root_for_migrations
) -> None:
    """State 3: the owner's own database, and every one created by an old initdb.

    create_all() builds the head but writes no version row, so alembic replays
    0001 against existing tables and dies -- permanently. The full ORM schema
    with no version row is unambiguous (create_all only ever builds the head),
    so it is stamped rather than reported.
    """
    database = tmp_path / "unstamped.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
    db_module.reset_engine()

    engine = db_module.get_engine()
    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
        connection.commit()
    db_module.reset_engine()

    result = db_module.upgrade_to_head()

    assert result["ok"], result["error"]
    with db_module.get_engine().connect() as connection:
        stamped = [
            row[0] for row in connection.exec_driver_sql("SELECT version_num FROM alembic_version")
        ]
    assert stamped == [result["revision"]]


def test_upgrade_to_head_refuses_to_guess_at_an_unrecognisable_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_root_for_migrations
) -> None:
    """Tables that are not ours and no version row: report it, do not stamp it.

    Stamping would claim migrations had run that had not, and the next real
    migration would then be skipped against a schema that needed it.
    """
    database = tmp_path / "foreign.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
    db_module.reset_engine()
    with db_module.get_engine().connect() as connection:
        connection.exec_driver_sql("CREATE TABLE somebody_elses_table (x INTEGER)")
        connection.commit()
    db_module.reset_engine()

    result = db_module.upgrade_to_head()

    assert not result["ok"]
    assert "alembic_version" in (result["error"] or "")
    with db_module.get_engine().connect() as connection:
        remaining = set(inspect(db_module.get_engine()).get_table_names())
    assert "alembic_version" not in remaining


def test_upgrade_to_head_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A container that exits on boot tells the owner nothing at all.

    There is no SMS any more, so a crash-looping deploy is completely silent.
    Coming up and rendering the error is the only way he finds out.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://nobody@127.0.0.1:1/nothing")
    db_module.reset_engine()
    try:
        result = db_module.upgrade_to_head()
        assert result["ok"] is False
        assert result["error"]
    finally:
        db_module.reset_engine()


# ==========================================================================
# The ephemeral-filesystem warning
# ==========================================================================


def test_sqlite_on_a_container_host_is_reported_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///data/towbook.db")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    db_module.reset_engine()

    warning = db_module.warn_if_ephemeral_sqlite()

    assert warning is not None
    assert "EPHEMERAL" in warning
    assert "DELETED" in warning


def test_sqlite_on_a_laptop_is_not_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local development is the supported use of SQLite; it must stay quiet."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///data/towbook.db")
    for name in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RENDER", "FLY_APP_NAME", "DYNO"):
        monkeypatch.delenv(name, raising=False)
    assert db_module.warn_if_ephemeral_sqlite() is None


def test_postgres_on_a_container_host_is_not_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@host/db")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    db_module.reset_engine()
    try:
        assert db_module.warn_if_ephemeral_sqlite() is None
    finally:
        db_module.reset_engine()


# ==========================================================================
# Notifications are off, and the routing table is still there
# ==========================================================================


def _shipped_notifications() -> dict:
    return yaml.safe_load((REAL_REPO_ROOT / "config" / "notifications.yaml").read_text("utf-8"))


def test_the_shipped_config_sends_no_sms_and_no_email() -> None:
    """The board is the delivery. Nothing may go out of the box.

    This is not a style preference: this repo is being handed to other towing
    companies, and a route left on bills the next owner for texts they never
    asked for, to a phone number from somebody else's environment.
    """
    routes = _shipped_notifications()["routes"]
    assert routes, "the routing table must not be deleted, only disabled"
    enabled = [route for route in routes if route.get("enabled") is not False]
    assert enabled == [], f"these routes would send on a fresh deploy: {enabled}"


def test_the_routing_table_is_intact_so_a_channel_is_one_yaml_edit_away() -> None:
    """Disabled, not deleted -- otherwise re-enabling means rebuilding the file."""
    document = _shipped_notifications()
    routes = document["routes"]

    covered_reports = {route.get("report") for route in routes if route.get("report")}
    assert {"hourly", "daily", "weekly", "monthly"} <= covered_reports

    covered_events = {route.get("event") for route in routes if route.get("event")}
    assert {"alert", "pipeline_failure"} <= covered_events

    # And everything a route needs in order to work when it is switched on.
    for route in routes:
        assert route.get("channel") in {"sms", "email"}
        assert route.get("to"), f"route with no recipients: {route}"
    assert document["recipients"]["owner"]["phone_env"] == "OWNER_PHONE"
    assert "pipeline_failure" in document["non_suppressible_events"]


def test_a_route_is_enabled_unless_it_says_otherwise(notifier) -> None:
    """Absent means on. A route added without the key must not be silently dead."""
    assert notifier.route_enabled({"report": "daily"}) is True
    assert notifier.route_enabled({"report": "daily", "enabled": True}) is True
    assert notifier.route_enabled({"report": "daily", "enabled": False}) is False
    assert notifier.route_enabled({"report": "daily", "enabled": "false"}) is False
    assert notifier.route_enabled({"report": "daily", "enabled": "no"}) is False
    assert notifier.route_enabled({"report": "daily", "enabled": "true"}) is True


def test_a_disabled_route_sends_nothing_but_is_still_recorded(
    notifier, write_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silence has to be attributable, or it is indistinguishable from a bug."""
    sent: list = []
    monkeypatch.setattr(notifier, "_send_one", lambda **kwargs: sent.append(kwargs))

    from towbook_agent.core.config_loader import get_notifications

    config = dict(get_notifications())
    config["routes"] = [
        {"report": "hourly", "channel": "sms", "enabled": False, "to": ["owner"],
         "template": "hourly_short"}
    ]
    write_config("notifications", config)

    notifier.dispatch_report("hourly", {"offered": 10, "accepted": 7, "rate": 0.7})

    assert sent == []
    from conftest import count_rows
    from towbook_agent.core.models import AlertFired

    assert count_rows(AlertFired) == 1
    from towbook_agent.core.db import get_session

    with get_session(commit=False) as session:
        from sqlalchemy import select

        row = session.execute(select(AlertFired)).scalars().one()
    # "delivery_disabled", never "no_matching_route": the difference between a
    # standing decision and a misconfiguration.
    assert row.suppressed_reason == "delivery_disabled"


def test_a_genuinely_unrouted_report_still_complains(
    notifier, write_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from towbook_agent.core.config_loader import get_notifications

    config = dict(get_notifications())
    config["routes"] = []
    write_config("notifications", config)

    notifier.dispatch_report("hourly", {"offered": 1, "accepted": 1, "rate": 1.0})

    from sqlalchemy import select

    from towbook_agent.core.db import get_session
    from towbook_agent.core.models import AlertFired

    with get_session(commit=False) as session:
        row = session.execute(select(AlertFired)).scalars().one()
    assert row.suppressed_reason == "no_matching_route"


# ==========================================================================
# The banner that replaced the text message
# ==========================================================================


def test_the_banner_is_absent_when_the_pipeline_is_healthy() -> None:
    """A banner that is always there is furniture and gets read as furniture."""
    from towbook_agent.web import queries as q

    assert q.pipeline_banner() is None


def test_a_failed_run_puts_a_banner_on_every_tab() -> None:
    from towbook_agent.core.db import get_session
    from towbook_agent.core.models import Run, utcnow
    from towbook_agent.web import queries as q

    with get_session() as session:
        session.add(
            Run(
                run_id="failed-1",
                report_type="hourly",
                status="failed",
                started_at=utcnow(),
                finished_at=utcnow(),
                error_message="acquisition: TimeoutError: the portal did not respond",
            )
        )

    banner = q.pipeline_banner()

    assert banner is not None
    assert banner["level"] == "error"
    assert "failed" in banner["title"].lower()
    assert "TimeoutError" in banner["detail"]


def test_a_recorded_pipeline_failure_reaches_the_banner_when_delivery_is_off() -> None:
    """With every route disabled, alerts_fired IS the delivery path."""
    from towbook_agent.core.db import get_session
    from towbook_agent.core.models import AlertFired, utcnow
    from towbook_agent.web import queries as q

    with get_session() as session:
        session.add(
            AlertFired(
                alert_id="pipeline_failure",
                entity="ingestion",
                severity="high",
                fired_at=utcnow(),
                payload={"stage": "ingestion", "error": "header drift: Service Needed missing"},
                suppressed_reason="delivery_disabled",
            )
        )

    banner = q.pipeline_banner()

    assert banner is not None
    assert "header drift" in banner["detail"]
    assert "only warning" in (banner["hint"] or "")


def test_the_banner_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """It renders on every page. It must not be able to take the board down."""
    from towbook_agent.web import queries as q

    monkeypatch.setattr(q, "last_run_summary", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert q.pipeline_banner() is None


def test_base_html_includes_the_banner() -> None:
    """The include is load-bearing: it is the only failure channel that is left."""
    base = (REAL_REPO_ROOT / "towbook_agent" / "web" / "templates" / "base.html").read_text("utf-8")
    assert "partials/pipeline_banner.html" in base


# ==========================================================================
# The scheduler has to actually run
# ==========================================================================


def test_the_scheduler_runs_unless_it_is_explicitly_turned_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default ON. A stale board is silent; a duplicated idempotent run is not."""
    from towbook_agent.core import scheduler

    monkeypatch.delenv("RUN_SCHEDULER", raising=False)
    assert scheduler.run_scheduler_enabled() is True
    for value in ("true", "1", "yes", "on", "TRUE"):
        monkeypatch.setenv("RUN_SCHEDULER", value)
        assert scheduler.run_scheduler_enabled() is True
    for value in ("false", "0", "no", "off"):
        monkeypatch.setenv("RUN_SCHEDULER", value)
        assert scheduler.run_scheduler_enabled() is False


def test_the_scheduler_can_be_split_into_its_own_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RUN_SCHEDULER=false must leave the web process serving and scheduling nothing."""
    from towbook_agent.core import scheduler

    monkeypatch.setenv("RUN_SCHEDULER", "false")
    status = scheduler.start_background_scheduler()

    assert status["running"] is False
    assert "RUN_SCHEDULER" in status["reason"]
    assert scheduler.background_scheduler_status()["running"] is False


def test_the_in_process_scheduler_starts_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    from towbook_agent.core import scheduler

    monkeypatch.setenv("RUN_SCHEDULER", "true")
    status = scheduler.start_background_scheduler(dry_run=True)
    try:
        assert status["running"] is True
        assert status["jobs"] >= 1, "schedule.yaml produced no jobs in the web process"
        live = scheduler.background_scheduler_status()
        assert live["running"] is True
        assert live["jobs"], "no job table in the running scheduler"
    finally:
        scheduler.stop_background_scheduler()
    assert scheduler.background_scheduler_status()["running"] is False


def test_starting_the_scheduler_twice_does_not_start_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from towbook_agent.core import scheduler

    monkeypatch.setenv("RUN_SCHEDULER", "true")
    scheduler.start_background_scheduler(dry_run=True)
    try:
        second = scheduler.start_background_scheduler(dry_run=True)
        assert second["running"] is True
        assert second["reason"] == "already running"
    finally:
        scheduler.stop_background_scheduler()


def test_health_reports_whether_this_container_is_scheduling_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"Serving but not scheduling" is the failure that looks most like health."""
    from towbook_agent.web import queries as q

    monkeypatch.setenv("RUN_SCHEDULER", "false")
    data = q.health_view()
    assert data["scheduler"]["enabled"] is False
    assert data["scheduler"]["running"] is False
    assert "overdue" in data
    assert "backend" in data["database"]


def test_the_startup_hook_migrates_and_schedules_before_serving(
    repo_root_for_migrations, booted_client
) -> None:
    """The whole boot sequence, through the ASGI lifespan the platform triggers.

    Railway's probe hits ``/healthz``, and it must answer more than "the port is
    open": on this deployment "up" and "up, migrated and scheduling" are
    different states and only the second one is useful. The probe is also exempt
    from the password gate, or the health check would fail on every boot forever.
    """
    payload = booted_client.get("/healthz").json()

    assert payload["ok"] is True
    assert payload["migration"]["ok"] is True
    assert payload["migration"]["revision"], "the lifespan did not run alembic"
    assert payload["database"]["backend"] == "sqlite"
    assert payload["scheduler"]["running"] is True, "the board would go stale unannounced"


def test_the_advisory_lock_key_is_stable_and_namespaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two containers of the same release must compute the same key, or it guards nothing."""
    from towbook_agent.core.leader import advisory_lock_key

    monkeypatch.delenv("LOCK_NAMESPACE", raising=False)
    first = advisory_lock_key()
    assert first == advisory_lock_key()
    assert -(2**63) <= first <= 2**63 - 1

    monkeypatch.setenv("LOCK_NAMESPACE", "second-deployment")
    assert advisory_lock_key() != first


def test_sqlite_needs_no_lease_and_says_why() -> None:
    """SQLite is one machine and a developer who started the process on purpose."""
    from towbook_agent.core.leader import acquire_scheduler_lease

    lease = acquire_scheduler_lease()
    try:
        assert lease.acquired is True
        assert "uvicorn worker" in lease.reason
    finally:
        lease.release()


def test_the_timezone_database_is_an_unconditional_dependency() -> None:
    """tzdata must not be marked Windows-only, because the container is Linux.

    ``TZ=America/Detroit`` is the deployment's most load-bearing variable after
    DATABASE_URL: the covered-vs-uncovered split is the headline of every
    report and it is a claim about local clock hours. ``queries.local_tz()``
    swallows a failed zone lookup and returns UTC, so on an image with no
    /usr/share/zoneinfo every day boundary silently moves four or five hours
    and every page still renders, confidently, with the wrong numbers.

    A ``platform_system == "Windows"`` marker means pip installs nothing on the
    box where the failure is invisible.
    """
    text = (REAL_REPO_ROOT / "requirements.txt").read_text("utf-8")
    active = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    tz_lines = [line for line in active if line.lower().startswith("tzdata")]
    assert tz_lines, "tzdata is not in requirements.txt"
    for line in tz_lines:
        assert ";" not in line, (
            f"tzdata carries an environment marker ({line!r}), so it is not "
            "installed on Linux -- where a missing tz database fails silently"
        )


def test_the_configured_timezone_really_resolves() -> None:
    """A zone name must produce that zone, not a silent UTC fallback.

    Asserted against a zone with a non-zero offset, because UTC-vs-Detroit is
    exactly the difference this catches and comparing names alone would not.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("America/Detroit")
    offset = datetime(2026, 7, 28, 12, 0, tzinfo=zone).utcoffset()
    assert offset is not None and offset.total_seconds() == -4 * 3600, (
        f"America/Detroit resolved to {offset}, not EDT; the IANA database is "
        "missing and every local day boundary is wrong"
    )


def test_overdue_reports_is_read_only() -> None:
    """The board asks this on every page load; it must not emit an alert.

    watchdog_check() shouts and keeps a cooldown. If the two shared one
    function, either the banner would silence the alert or opening the board
    would fire one per page view.
    """
    from towbook_agent.core import events as events_module
    from towbook_agent.core.scheduler import overdue_reports

    emitted: list = []
    handler = lambda kind, payload: emitted.append(kind)  # noqa: E731
    events_module.register_handler(handler)
    try:
        overdue_reports()
    finally:
        events_module.unregister_handler(handler)
    assert emitted == []


# ==========================================================================
# The API acquisition path must not need a browser
# ==========================================================================


def test_the_default_acquisition_path_imports_no_playwright() -> None:
    """A container with no browsers has to run the whole system.

    ``source: api`` is the default and talks plain HTTP. If anything on that
    path imported playwright at module scope, the deploy would fail at import
    time -- on the first scheduled run, in a background thread, an hour after
    the deploy looked successful.
    """
    import subprocess
    import sys

    probe = (
        "import sys;"
        "import towbook_agent.agents.acquisition_api;"
        "import towbook_agent.agents.ingestion;"
        "import towbook_agent.core.scheduler;"
        "import towbook_agent.web.app;"
        "loaded=[m for m in sys.modules if m.split('.')[0]=='playwright'];"
        "print('PLAYWRIGHT_LOADED' if loaded else 'CLEAN')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(REAL_REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "CLEAN" in result.stdout, result.stdout


def test_playwright_is_not_a_hard_dependency() -> None:
    """It is an extra, so the container image carries neither wheel nor browser."""
    text = (REAL_REPO_ROOT / "requirements.txt").read_text("utf-8")
    active = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any(line.lower().startswith("playwright") for line in active)
    assert any(line.lower().startswith("psycopg") for line in active)

    pyproject = (REAL_REPO_ROOT / "pyproject.toml").read_text("utf-8")
    assert 'ui = [' in pyproject and '"playwright' in pyproject


# ==========================================================================
# The deployment manifests themselves
# ==========================================================================


def _uncommented_lines(path: Path) -> str:
    """The file with every ``#`` comment line dropped.

    The deployment manifests carry long comments that quote the exact settings
    they warn against, so a plain substring search over the raw text reports the
    warning as though it were the setting. Only active lines are configuration.
    """
    return "\n".join(
        line for line in path.read_text("utf-8").splitlines() if not line.strip().startswith("#")
    )


def test_the_deployment_files_exist() -> None:
    for name in ("railway.json", "Procfile", "nixpacks.toml", "start.py", ".python-version"):
        assert (REAL_REPO_ROOT / name).is_file(), f"{name} is missing"


def test_railway_json_is_valid_and_asks_for_one_replica() -> None:
    """Two replicas is two schedulers. The lock catches it; the config prevents it."""
    config = json.loads((REAL_REPO_ROOT / "railway.json").read_text("utf-8"))
    assert config["deploy"]["numReplicas"] == 1
    assert config["deploy"]["healthcheckPath"] == "/healthz"
    assert config["deploy"]["startCommand"] == "python start.py"
    assert config["build"]["builder"] == "NIXPACKS"


def test_nothing_overrides_the_providers_install_phase() -> None:
    """The Nixpacks Python provider owns the install, because only it makes the venv.

    This is a regression test for a build that failed on every push. railway.json
    used to carry `buildCommand: python -m pip install -r requirements.txt` and
    nixpacks.toml used to replace `[phases.install]` with the same thing. Both
    ran against the interpreter in the read-only Nix store rather than the venv
    at /opt/venv, and the Nix python package ships no pip, so the image never
    built:

        /root/.nix-profile/bin/python: No module named pip

    Left to itself the provider creates /opt/venv, installs requirements.txt into
    it and puts it on PATH. An override here silently takes back all three.
    """
    config = json.loads((REAL_REPO_ROOT / "railway.json").read_text("utf-8"))
    assert "buildCommand" not in config["build"], (
        "railway.json's buildCommand replaces the Nixpacks build phase; the "
        "provider already installs requirements.txt into /opt/venv"
    )

    active = _uncommented_lines(REAL_REPO_ROOT / "nixpacks.toml")
    assert "[phases.install]" not in active, (
        "a [phases.install] table replaces the provider's install commands, "
        "including the venv creation the start command depends on"
    )
    assert "ensurepip" not in active, (
        "ensurepip installs into the read-only Nix store, not /opt/venv"
    )


def test_the_start_command_is_the_same_everywhere() -> None:
    """railway.json, nixpacks.toml and the Procfile must not drift apart."""
    railway = json.loads((REAL_REPO_ROOT / "railway.json").read_text("utf-8"))
    nixpacks = (REAL_REPO_ROOT / "nixpacks.toml").read_text("utf-8")
    procfile = (REAL_REPO_ROOT / "Procfile").read_text("utf-8")

    command = railway["deploy"]["startCommand"]
    assert f'cmd = "{command}"' in nixpacks
    assert f"web: {command}" in procfile


def test_no_process_definition_asks_for_more_than_one_worker() -> None:
    """N uvicorn workers would be N schedulers, each pulling Towbook on the cron."""
    for name in ("Procfile", "railway.json", "nixpacks.toml"):
        text = (REAL_REPO_ROOT / name).read_text("utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            assert "--workers" not in stripped, f"{name}: {stripped}"


def test_the_python_pin_lives_in_one_file_and_agrees_with_pyproject() -> None:
    """`.python-version` is the only pin. Nixpacks, pyenv and uv all read it.

    It used to be pinned twice -- here and as `nixPkgs = ["python312", ...]` in
    nixpacks.toml -- which is how two numbers drift apart, and the nixPkgs half
    is what discarded the provider's packages and broke the build. Nixpacks reads
    `.python-version` itself (after $NIXPACKS_PYTHON_VERSION, ahead of
    runtime.txt), so the second pin bought nothing.
    """
    pinned = (REAL_REPO_ROOT / ".python-version").read_text("utf-8").strip()
    pyproject = (REAL_REPO_ROOT / "pyproject.toml").read_text("utf-8")

    major, minor = pinned.split(".")[:2]
    assert 'requires-python = ">=3.11"' in pyproject
    assert (int(major), int(minor)) >= (3, 11)

    # Comments may discuss the old override; an active line must not restore it.
    for line in _uncommented_lines(REAL_REPO_ROOT / "nixpacks.toml").splitlines():
        assert not re.search(r"\bpython\d{2,3}\b", line), (
            f"nixpacks.toml pins the interpreter again: {line.strip()!r} -- "
            "the pin belongs in .python-version alone"
        )


def test_env_example_documents_every_variable_the_deployment_reads() -> None:
    """A variable that only exists in code is a variable nobody sets."""
    text = (REAL_REPO_ROOT / ".env.example").read_text("utf-8")
    for name in (
        "DATABASE_URL",
        "TZ",
        "TOWBOOK_USER",
        "TOWBOOK_PASS",
        "DASHBOARD_PASSWORD",
        "SESSION_SECRET",
        "RUN_SCHEDULER",
        "BOOTSTRAP_ON_EMPTY",
        "BOOTSTRAP_DAYS",
        "LOCK_NAMESPACE",
        "POSTGRES_DRIVER",
        "DB_POOL_SIZE",
        "LOG_LEVEL",
        "ANTHROPIC_API_KEY",
    ):
        assert name in text, f"{name} is not documented in .env.example"


def test_env_example_says_sqlite_must_not_be_used_on_railway() -> None:
    """The single most expensive mistake available here, in the file people read."""
    text = (REAL_REPO_ROOT / ".env.example").read_text("utf-8").upper()
    assert "SQLITE MUST NOT BE USED ON RAILWAY" in text
    assert "EPHEMERAL" in text


def test_the_readme_has_a_railway_section() -> None:
    text = (REAL_REPO_ROOT / "README.md").read_text("utf-8")
    assert "## Deploying to Railway" in text
    for needle in ("DATABASE_URL", "PostgreSQL", "RUN_SCHEDULER", "DASHBOARD_PASSWORD"):
        assert needle in text, f"the Railway section does not mention {needle}"


def test_port_and_host_resolution_prefers_the_platforms_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ignoring $PORT binds where the router is not looking: every request 502s."""
    from towbook_agent.web.app import resolve_host, resolve_port

    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("DASHBOARD_PORT", raising=False)
    monkeypatch.delenv("DASHBOARD_HOST", raising=False)
    for name in ("RAILWAY_ENVIRONMENT", "RENDER", "FLY_APP_NAME", "DYNO"):
        monkeypatch.delenv(name, raising=False)

    assert resolve_port() == 8080
    assert resolve_host() == "127.0.0.1"

    monkeypatch.setenv("DASHBOARD_PORT", "9001")
    assert resolve_port() == 9001

    monkeypatch.setenv("PORT", "7777")
    assert resolve_port() == 7777, "PORT must win: the platform assigns it"
    assert resolve_port(1234) == 1234, "an explicit --port must win over everything"

    monkeypatch.setenv("PORT", "not-a-number")
    assert resolve_port() == 9001, "a junk PORT falls through rather than crashing"

    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert resolve_host() == "0.0.0.0", "a container must bind every interface"
    monkeypatch.setenv("DASHBOARD_HOST", "10.0.0.5")
    assert resolve_host() == "10.0.0.5"
    assert resolve_host("0.0.0.0") == "0.0.0.0"
