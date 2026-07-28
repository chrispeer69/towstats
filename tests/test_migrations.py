"""The migration and the models must describe the same database.

``init_db()`` creates the schema from the ORM metadata; alembic creates it from
a migration script. If those two ever disagree, a fresh install and an upgraded
install run different code against different tables -- and the difference shows
up as a missing index under load, months later. So they are compared here,
table by table, index by index, constraint by constraint.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from conftest import REAL_REPO_ROOT
from towbook_agent.core.models import Base

alembic_config = pytest.importorskip("alembic.config")
alembic_command = pytest.importorskip("alembic.command")

ALEMBIC_DIR = REAL_REPO_ROOT / "alembic"
ALEMBIC_INI = REAL_REPO_ROOT / "alembic.ini"


def _config(url: str) -> "alembic_config.Config":
    config = alembic_config.Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture
def migrated(tmp_path: Path):
    """A database built by ``alembic upgrade head``."""
    database = tmp_path / "migrated.db"
    url = _sqlite_url(database)
    alembic_command.upgrade(_config(url), "head")
    engine = create_engine(url, future=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def created(tmp_path: Path):
    """The same schema, built straight from the ORM metadata."""
    database = tmp_path / "created.db"
    engine = create_engine(_sqlite_url(database), future=True)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


# --------------------------------------------------------------------------


def test_the_alembic_directory_is_wired_up() -> None:
    assert ALEMBIC_INI.is_file(), "alembic.ini must live at the repo root"
    assert (ALEMBIC_DIR / "env.py").is_file()
    assert (ALEMBIC_DIR / "script.py.mako").is_file()
    versions = list((ALEMBIC_DIR / "versions").glob("*.py"))
    assert versions, "there is no migration to run"


def test_there_is_exactly_one_head() -> None:
    """Two heads means somebody branched and did not merge; upgrade would fail."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_config("sqlite://"))
    assert len(script.get_heads()) == 1


def test_migration_creates_every_table(migrated) -> None:
    tables = set(inspect(migrated).get_table_names())
    expected = set(Base.metadata.tables) | {"alembic_version"}
    assert tables == expected


def test_migration_matches_the_models_column_for_column(migrated, created) -> None:
    from_migration = inspect(migrated)
    from_models = inspect(created)

    for table in sorted(Base.metadata.tables):
        migrated_columns = {
            column["name"]: (str(column["type"]).upper(), bool(column["nullable"]))
            for column in from_migration.get_columns(table)
        }
        model_columns = {
            column["name"]: (str(column["type"]).upper(), bool(column["nullable"]))
            for column in from_models.get_columns(table)
        }
        assert migrated_columns == model_columns, f"{table} differs from the models"


def test_migration_creates_every_index(migrated, created) -> None:
    from_migration = inspect(migrated)
    from_models = inspect(created)

    for table in sorted(Base.metadata.tables):
        migrated_indexes = {
            (index["name"], tuple(index["column_names"]))
            for index in from_migration.get_indexes(table)
        }
        model_indexes = {
            (index["name"], tuple(index["column_names"]))
            for index in from_models.get_indexes(table)
        }
        assert migrated_indexes == model_indexes, f"indexes on {table} differ"


def test_migration_creates_every_unique_constraint(migrated, created) -> None:
    """The metrics window keys are what make recomputation an upsert."""
    from_migration = inspect(migrated)
    from_models = inspect(created)

    for table in sorted(Base.metadata.tables):
        migrated_unique = {
            (constraint["name"], tuple(constraint["column_names"]))
            for constraint in from_migration.get_unique_constraints(table)
        }
        model_unique = {
            (constraint["name"], tuple(constraint["column_names"]))
            for constraint in from_models.get_unique_constraints(table)
        }
        assert migrated_unique == model_unique, f"unique constraints on {table} differ"


def test_primary_keys_match(migrated, created) -> None:
    from_migration = inspect(migrated)
    from_models = inspect(created)

    for table in sorted(Base.metadata.tables):
        assert (
            from_migration.get_pk_constraint(table)["constrained_columns"]
            == from_models.get_pk_constraint(table)["constrained_columns"]
        ), f"primary key of {table} differs"


def test_the_required_spec_indexes_exist(migrated) -> None:
    """The four indexes the spec names explicitly, by name."""
    names = {index["name"] for index in inspect(migrated).get_indexes("requests")}
    assert {
        "ix_requests_offered_at",
        "ix_requests_client_key_offered_at",
        "ix_requests_status",
        "ix_requests_service_class",
    } <= names


def test_autogenerate_finds_nothing_left_to_do(tmp_path: Path) -> None:
    """``alembic check`` against a freshly migrated database must be silent.

    This is the strongest form of the claim the tests above make column by
    column: alembic itself, doing its own autogenerate comparison, finds no
    difference between the migrated schema and the models.
    """
    database = tmp_path / "check.db"
    config = _config(_sqlite_url(database))
    alembic_command.upgrade(config, "head")

    try:
        alembic_command.check(config)
    except Exception as exc:  # AutogenerateDiffsDetected, or nothing at all
        pytest.fail(f"the migration has drifted from the models: {exc}")


def test_upgrade_then_downgrade_leaves_nothing_behind(tmp_path: Path) -> None:
    database = tmp_path / "roundtrip.db"
    config = _config(_sqlite_url(database))

    alembic_command.upgrade(config, "head")
    alembic_command.downgrade(config, "base")

    engine = create_engine(_sqlite_url(database), future=True)
    try:
        remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()
    assert remaining == set(), f"downgrade left tables behind: {sorted(remaining)}"


def test_upgrade_is_reapplicable_after_downgrade(tmp_path: Path) -> None:
    database = tmp_path / "again.db"
    config = _config(_sqlite_url(database))

    alembic_command.upgrade(config, "head")
    alembic_command.downgrade(config, "base")
    alembic_command.upgrade(config, "head")

    engine = create_engine(_sqlite_url(database), future=True)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert set(Base.metadata.tables) <= tables


# --------------------------------------------------------------------------
# init_db() and alembic have to agree about what revision they built
# --------------------------------------------------------------------------


def test_a_database_created_by_init_db_can_still_be_migrated(tmp_path: Path, monkeypatch) -> None:
    """The bootstrap path must leave the database under alembic's control.

    ``create_all`` builds the schema from the ORM metadata -- which IS the head
    -- but writes no ``alembic_version`` row. Alembic then still believes the
    database is at base, so the next ``alembic upgrade head`` replays 0001
    against tables that already exist and dies on "table requests already
    exists". Permanently: there is no state it recovers from on its own.

    Every database this system bootstrapped was in exactly that state, the
    owner's included. It only surfaces when a SECOND migration is added, which
    is the worst possible moment for it to surface.

    REPO_ROOT is repointed at the real repo because that is where the migration
    scripts live; the sandbox has no alembic/ directory, and with none there is
    nothing to stamp against.
    """
    from towbook_agent.core import db as db_module
    from towbook_agent.core import paths as paths_module

    database = tmp_path / "bootstrapped.db"
    monkeypatch.setattr(paths_module, "REPO_ROOT", REAL_REPO_ROOT)
    monkeypatch.setenv("DATABASE_URL", _sqlite_url(database))
    db_module.reset_engine()
    try:
        db_module.init_db()

        engine = create_engine(_sqlite_url(database), future=True)
        try:
            tables = set(inspect(engine).get_table_names())
            with engine.connect() as connection:
                stamped = list(
                    connection.exec_driver_sql("select version_num from alembic_version").scalars()
                )
        finally:
            engine.dispose()

        assert "alembic_version" in tables, (
            "init_db() created a database with no alembic_version row; "
            "`alembic upgrade head` can never run against it"
        )

        from alembic.script import ScriptDirectory

        head = ScriptDirectory.from_config(_config("sqlite://")).get_heads()[0]
        assert stamped == [head], f"stamped {stamped!r}, head is {head!r}"

        # And the payoff: upgrading is now a clean no-op, not an exception.
        alembic_command.upgrade(_config(_sqlite_url(database)), "head")
    finally:
        db_module.reset_engine()


def test_init_db_does_not_stamp_a_database_that_already_had_tables(
    tmp_path: Path, monkeypatch
) -> None:
    """A pre-existing unversioned schema is ambiguous -- do not guess at it.

    Nobody can tell which revision an unstamped, already-populated database
    corresponds to. Stamping it head would claim migrations had run that had
    not. It is reported instead, and left alone.
    """
    from towbook_agent.core import db as db_module
    from towbook_agent.core import paths as paths_module

    database = tmp_path / "preexisting.db"
    engine = create_engine(_sqlite_url(database), future=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("create table requests (request_id text primary key)")
    finally:
        engine.dispose()

    monkeypatch.setattr(paths_module, "REPO_ROOT", REAL_REPO_ROOT)
    monkeypatch.setenv("DATABASE_URL", _sqlite_url(database))
    db_module.reset_engine()
    try:
        db_module.init_db()
        engine = create_engine(_sqlite_url(database), future=True)
        try:
            tables = set(inspect(engine).get_table_names())
        finally:
            engine.dispose()
        assert "alembic_version" not in tables, (
            "init_db() stamped a database whose real revision it cannot know"
        )
    finally:
        db_module.reset_engine()
