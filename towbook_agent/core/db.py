"""Database engine, session factory and schema creation.

The engine is built lazily from ``DATABASE_URL`` (default
``sqlite:///data/towbook.db``) so that importing this module never touches the
filesystem -- tests can point DATABASE_URL somewhere else and call
:func:`reset_engine` without fighting import order.

A relative SQLite path is resolved against REPO_ROOT rather than the current
working directory. The scheduler, the CLI and the web server are all started
from different places; they must all open the same database file.

TWO BACKENDS, ONE SCHEMA
------------------------
* **SQLite** -- local development and the whole test suite. Fast, offline, one
  file, and the pragmas below (WAL, foreign_keys, busy_timeout) are what make it
  survive a scheduler writing while the dashboard reads.
* **PostgreSQL** -- every hosted deployment, Railway included. **A Railway
  container has an ephemeral filesystem: a SQLite file under ``data/`` is wiped
  on every redeploy, silently, taking all history with it.** See
  :func:`warn_if_ephemeral_sqlite`, which says so out loud at boot.

Railway hands the database over as ``DATABASE_URL``. Two things about that
string have to be fixed before SQLAlchemy sees it, and :func:`normalize_database_url`
is the one place that happens:

1. the legacy ``postgres://`` scheme, which SQLAlchemy 2.0 refuses outright;
2. the driver. Bare ``postgresql://`` means psycopg2, which is not installed --
   this project ships ``psycopg[binary]`` (psycopg 3), so the URL is rewritten to
   ``postgresql+psycopg://``.

Nothing else in the codebase is allowed to know which backend it is talking to.
The upsert in ``agents/ingestion.py`` picks the dialect-appropriate INSERT from
the bound session, every pragma below is behind a SQLite check, and every column
type in ``core/models.py`` is a portable SQLAlchemy type.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from .models import Base
from .paths import DATA_DIR, ensure_dirs, resolve_under_root

__all__ = [
    "DEFAULT_DATABASE_URL",
    "DEFAULT_POSTGRES_DRIVER",
    "normalize_database_url",
    "database_url",
    "backend_name",
    "is_sqlite",
    "is_postgres",
    "get_engine",
    "get_session",
    "SessionLocal",
    "init_db",
    "upgrade_to_head",
    "reset_engine",
    "dispose_engine",
    "healthcheck",
    "warn_if_ephemeral_sqlite",
]

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL: str = "sqlite:///data/towbook.db"

#: psycopg 3. Shipped as ``psycopg[binary]`` so there is no build toolchain to
#: install in the container. Override with POSTGRES_DRIVER=psycopg2 if a company
#: deploying this already standardises on psycopg2.
DEFAULT_POSTGRES_DRIVER: str = "psycopg"

#: Both spellings Railway / Heroku / Render have used for the same thing.
#: ``postgres://`` is the legacy one and SQLAlchemy 2.0 raises on it rather than
#: guessing, which turns a working paste of the provider's own value into a
#: crash at boot.
_POSTGRES_SCHEMES: frozenset[str] = frozenset({"postgres", "postgresql"})

_engine: Engine | None = None
_engine_url: str | None = None


# --------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------


def normalize_database_url(raw_url: str) -> str:
    """Make a provider-supplied connection string usable by SQLAlchemy 2.0.

    Two rewrites, both of them for PostgreSQL and both of them things that would
    otherwise fail at boot on a fresh deployment:

    * ``postgres://...`` -> ``postgresql+psycopg://...``. SQLAlchemy removed the
      ``postgres`` alias; Railway's dashboard still shows URLs in that form.
    * ``postgresql://...`` -> ``postgresql+psycopg://...``. The bare scheme means
      psycopg2, which this project does not install.

    An explicit driver (``postgresql+psycopg2://``, ``postgresql+asyncpg://``) is
    left exactly as written -- if somebody named a driver, they meant it. Every
    non-PostgreSQL URL, SQLite included, is returned untouched.
    """
    text = (raw_url or "").strip()
    scheme, separator, rest = text.partition("://")
    if not separator:
        return text

    name, _, driver = scheme.partition("+")
    if name.strip().lower() not in _POSTGRES_SCHEMES:
        return text
    if driver:
        return text

    driver = (os.environ.get("POSTGRES_DRIVER") or "").strip() or DEFAULT_POSTGRES_DRIVER
    return f"postgresql+{driver}://{rest}"


def database_url() -> str:
    """Return the configured DATABASE_URL, normalised, or the default.

    Every consumer -- the engine, alembic's ``env.py``, the CLI -- goes through
    here, so the scheme rewrite in :func:`normalize_database_url` happens once
    and cannot be skipped by one of them.
    """
    raw = (os.environ.get("DATABASE_URL") or "").strip() or DEFAULT_DATABASE_URL
    return normalize_database_url(raw)


def backend_name(url: str | None = None) -> str:
    """The SQLAlchemy backend name -- ``sqlite``, ``postgresql``, ... ."""
    try:
        return make_url(url or database_url()).get_backend_name()
    except Exception:  # pragma: no cover - an unparseable URL is reported later
        return ""


def is_sqlite(url: str | None = None) -> bool:
    return backend_name(url).startswith("sqlite")


def is_postgres(url: str | None = None) -> bool:
    return backend_name(url).startswith("postgres")


def _prepare_url(raw_url: str) -> URL:
    """Parse the URL and, for SQLite, make the path absolute and create dirs."""
    url = make_url(raw_url)

    if not url.get_backend_name().startswith("sqlite"):
        return url

    database = url.database
    if not database or database == ":memory:":
        return url  # in-memory, nothing to create

    path = resolve_under_root(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    return url.set(database=str(path))


def _sqlite_on_connect(dbapi_connection: Any, connection_record: Any) -> None:
    """Apply the SQLite pragmas this application depends on.

    WAL keeps the dashboard readable while the scheduler is writing.
    foreign_keys is off by default in SQLite and has to be enabled per
    connection. busy_timeout stops an hourly job and a dashboard request from
    colliding into an immediate "database is locked".

    **SQLite only.** ``PRAGMA`` is not SQL PostgreSQL understands -- issuing one
    on a Postgres connection is a syntax error that kills the connection before
    the first query is ever sent. That is why this listener is registered inside
    the ``is_sqlite`` branch of :func:`get_engine` and nowhere else; do not
    promote it to an unconditional ``event.listen``.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=10000")
    finally:
        cursor.close()


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip())
    except (TypeError, ValueError):
        return default


def warn_if_ephemeral_sqlite() -> str | None:
    """Say so, loudly, when a container is about to store history on a ramdisk.

    Railway, Render, Fly and every other container host give the process a
    filesystem that is thrown away on redeploy. ``sqlite:///data/towbook.db`` is
    the default, so a deployment that simply does not set DATABASE_URL *works* --
    the dashboard comes up, the scheduler runs, the numbers look right -- and
    then loses thirty days of offers the next time somebody pushes a commit. The
    failure is invisible at exactly the moment it happens, which is why this is a
    CRITICAL log line and a banner on the board rather than a comment in a
    README.

    Returns the warning text when the situation applies, otherwise None.
    """
    if not is_sqlite():
        return None
    # Set by Railway on every service. Other hosts set their own; PORT alone is
    # too weak a signal (a laptop can set it), so the check is provider-specific
    # and additive -- an unrecognised host simply gets no warning.
    hosted = any(
        os.environ.get(name)
        for name in (
            "RAILWAY_ENVIRONMENT",
            "RAILWAY_ENVIRONMENT_NAME",
            "RAILWAY_PROJECT_ID",
            "RENDER",
            "FLY_APP_NAME",
            "DYNO",
        )
    )
    if not hosted:
        return None
    message = (
        "DATABASE_URL is SQLite on a container host with an EPHEMERAL filesystem. "
        "Every offer, metric and run record will be DELETED on the next redeploy, "
        "with no error. Add a PostgreSQL database to this project and set "
        "DATABASE_URL to its connection string."
    )
    logger.critical(message)
    return message


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


def get_engine(echo: bool | None = None) -> Engine:
    """Return the process-wide Engine, creating it on first use.

    The engine is rebuilt automatically if DATABASE_URL changed since it was
    created, which keeps tests honest.
    """
    global _engine, _engine_url

    raw_url = database_url()
    if _engine is not None and _engine_url == raw_url:
        return _engine

    if _engine is not None:
        _engine.dispose()
        _engine = None

    url = _prepare_url(raw_url)
    if echo is None:
        echo = os.environ.get("SQL_ECHO", "").strip().lower() in {"1", "true", "yes", "on"}

    kwargs: dict[str, Any] = {"echo": echo, "future": True, "pool_pre_ping": True}

    backend = url.get_backend_name()
    sqlite = backend.startswith("sqlite")
    if sqlite:
        # The scheduler writes from a worker thread while the web app reads
        # from another; SQLAlchemy's pool hands connections across threads.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        # A hosted Postgres has a hard connection cap (Railway's starter plan is
        # low) and one web process now runs both the dashboard and the scheduler,
        # so the pool is bounded rather than left at SQLAlchemy's default. Recycle
        # before any sane proxy idle-timeout so a connection that was closed under
        # us is replaced instead of raising mid-request; pool_pre_ping above is
        # the second line of defence for the same failure.
        kwargs["pool_size"] = _int_env("DB_POOL_SIZE", 5)
        kwargs["max_overflow"] = _int_env("DB_MAX_OVERFLOW", 5)
        kwargs["pool_recycle"] = _int_env("DB_POOL_RECYCLE", 1800)
        kwargs["pool_timeout"] = _int_env("DB_POOL_TIMEOUT", 30)

    engine = create_engine(url, **kwargs)

    if sqlite:
        # SQLite pragmas only. See _sqlite_on_connect.
        event.listen(engine, "connect", _sqlite_on_connect)

    _engine = engine
    _engine_url = raw_url
    logger.debug("database engine created for %s", url.render_as_string(hide_password=True))
    return engine


def dispose_engine() -> None:
    """Close all pooled connections and drop the cached engine."""
    global _engine, _engine_url
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_url = None
    SessionLocal.reset()


#: Alias kept because "reset" reads better in tests than "dispose".
reset_engine = dispose_engine


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


class _LazySessionFactory:
    """``SessionLocal()`` that binds itself to the engine on first call.

    Exposed as the module level ``SessionLocal`` named in the module contract.
    Behaves like a sessionmaker: call it to get a Session.
    """

    def __init__(self) -> None:
        self._maker = sessionmaker(class_=Session, expire_on_commit=False, autoflush=False)
        self._bound_url: str | None = None

    def _ensure_bound(self) -> None:
        current = database_url()
        if self._bound_url != current:
            self._maker.configure(bind=get_engine())
            self._bound_url = current

    def __call__(self, **kwargs: Any) -> Session:
        self._ensure_bound()
        return self._maker(**kwargs)

    def configure(self, **kwargs: Any) -> None:
        self._maker.configure(**kwargs)
        if "bind" in kwargs:
            self._bound_url = database_url()

    def reset(self) -> None:
        self._bound_url = None


SessionLocal = _LazySessionFactory()


@contextmanager
def get_session(commit: bool = True) -> Iterator[Session]:
    """Session contextmanager: commits on success, rolls back on failure.

        with get_session() as session:
            session.add(request)

    Pass ``commit=False`` for read-only work.
    """
    session = SessionLocal()
    try:
        yield session
        if commit:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


def _alembic_config(url: str) -> Any | None:
    """An alembic Config pointed at this repo's migrations and this database."""
    try:
        from alembic.config import Config
    except ImportError:  # pragma: no cover - alembic is a hard dependency
        return None
    ini = resolve_under_root("alembic.ini")
    if not ini.is_file():
        return None
    config = Config(str(ini))
    config.set_main_option("script_location", str(resolve_under_root("alembic")))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _alembic_head(config: Any) -> str | None:
    """The single head revision, or None if there is not exactly one."""
    try:
        from alembic.script import ScriptDirectory

        heads = ScriptDirectory.from_config(config).get_heads()
    except Exception as exc:  # pragma: no cover - depends on the environment
        logger.debug("could not read the alembic head: %s", exc)
        return None
    return heads[0] if len(heads) == 1 else None


def _stamp_fresh_database(engine: Engine) -> None:
    """Record that a just-created database is already at the migration head.

    ``create_all`` builds the schema from the ORM metadata, which IS the head --
    but it writes no ``alembic_version`` row, so alembic still believes the
    database is at base. The next ``alembic upgrade head`` then replays 0001
    against tables that already exist and dies on "table requests already
    exists", permanently. Every database this system has ever bootstrapped was
    in that state: created by init_db, and impossible to migrate afterwards.

    Only a database that was EMPTY a moment ago is stamped. A database that
    already had tables and no version row is ambiguous -- nobody can tell which
    revision its schema corresponds to -- so it is reported, not guessed at.
    """
    url = engine.url.render_as_string(hide_password=False)
    config = _alembic_config(url)
    if config is None:
        return
    head = _alembic_head(config)
    if head is None:
        return
    try:
        from alembic import command

        command.stamp(config, head)
    except Exception as exc:  # pragma: no cover - depends on the environment
        logger.warning(
            "created a fresh database but could not stamp it at alembic revision "
            "%s (%s). `alembic upgrade head` will fail on it until it is stamped "
            "by hand: alembic stamp %s",
            head,
            exc,
            head,
        )
        return
    logger.info("stamped the new database at alembic revision %s", head)


def init_db(echo: bool | None = None) -> Engine:
    """Create the runtime directories and every table that does not exist yet.

    Idempotent. Safe to call at the start of any command. Alembic owns schema
    *changes*; this is the bootstrap path for a fresh database -- and, because
    it is, it also records the revision it built, so the database it produces
    can still be migrated later. See :func:`_stamp_fresh_database`.
    """
    ensure_dirs()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    engine = get_engine(echo=echo)

    inspector = sa_inspect(engine)
    existing = set(inspector.get_table_names())
    was_empty = not existing

    Base.metadata.create_all(engine)

    if was_empty:
        _stamp_fresh_database(engine)
    elif "alembic_version" not in existing:
        created = set(sa_inspect(engine).get_table_names()) - existing
        logger.warning(
            "%s has tables but no alembic_version row, so `alembic upgrade head` "
            "will replay the first migration against them and fail. create_all "
            "just added %s. Repair with: alembic stamp <the revision this schema "
            "matches>, then alembic upgrade head.",
            engine.url.render_as_string(hide_password=True),
            sorted(created) or "nothing",
        )

    logger.info(
        "database ready at %s (%d tables)",
        engine.url.render_as_string(hide_password=True),
        len(Base.metadata.tables),
    )
    return engine


def healthcheck() -> dict[str, Any]:
    """Return a small dict describing database reachability.

    Used by the dashboard and by ``login-check`` style diagnostics. Never
    raises: it reports the failure instead, because a health probe that throws
    is a health probe nobody calls.
    """
    result: dict[str, Any] = {
        "url": None,
        "ok": False,
        "tables": [],
        "error": None,
    }
    try:
        engine = get_engine()
        result["url"] = engine.url.render_as_string(hide_password=True)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            if engine.url.get_backend_name().startswith("sqlite"):
                rows = connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                )
                result["tables"] = [row[0] for row in rows]
        result["ok"] = True
    except Exception as exc:  # pragma: no cover - depends on environment
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def sqlite_file() -> Path | None:
    """Return the SQLite file backing the current engine, if it is SQLite."""
    url = _prepare_url(database_url())
    if url.get_backend_name().startswith("sqlite") and url.database and url.database != ":memory:":
        return Path(url.database)
    return None
