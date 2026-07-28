"""Alembic environment, wired to the application's own models and engine.

Two things matter here:

* ``target_metadata`` is ``towbook_agent.core.models.Base.metadata`` -- the same
  metadata ``init_db()`` creates from -- so ``--autogenerate`` compares against
  the real models and a migration can never drift from the ORM.
* the database URL comes from ``towbook_agent.core.db.database_url()``, which
  reads ``DATABASE_URL`` and resolves a relative SQLite path against the repo
  root. Alembic therefore opens exactly the file the application opens, no
  matter which directory it was invoked from, and no connection string is ever
  written into a committed file.

``render_as_batch`` is on for SQLite because SQLite cannot ALTER a column;
alembic emulates it by rebuilding the table. Without it, the first migration
that changes a column type would fail on the production database.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# The repo root, so `alembic` works from anywhere.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from towbook_agent.core.db import database_url, get_engine  # noqa: E402
from towbook_agent.core.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _literal(default: object) -> str | None:
    """Reduce a server default to bare text: ``('')`` and ``''`` both -> ``""``."""
    if default is None:
        return None
    text = str(getattr(default, "arg", default)).strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return text


def compare_server_default(
    context: object,
    inspected_column: object,
    metadata_column: object,
    inspected_default: object,
    metadata_default: object,
    rendered_metadata_default: object,
) -> bool | None:
    """Ignore quoting differences when comparing server defaults.

    SQLite reflects ``DEFAULT ''`` as the two-character string ``''`` while the
    model declares the zero-length string, so the stock comparison reports a
    difference that does not exist and every ``alembic check`` fails on a clean
    database. Returning False means "these are the same"; returning None hands
    a genuine difference back to alembic's own logic.
    """
    inspected = _literal(inspected_default)
    declared = _literal(
        rendered_metadata_default if rendered_metadata_default is not None else metadata_default
    )
    if inspected == declared:
        return False
    return None


def _override_url() -> str:
    """An explicit URL from ``-x url=...`` or from alembic.ini, if there is one."""
    arguments = context.get_x_argument(as_dictionary=True)
    explicit = (arguments.get("url") or "").strip()
    if explicit:
        return explicit
    return (config.get_main_option("sqlalchemy.url") or "").strip()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to anything."""
    url = _override_url() or database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=compare_server_default,
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live connection."""
    override = _override_url()
    if override:
        connectable = create_engine(override, poolclass=pool.NullPool, future=True)
    else:
        # The application's own engine: same URL, same SQLite pragmas.
        connectable = get_engine()

    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=compare_server_default,
            render_as_batch=is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
