"""Railway entrypoint: migrate, bootstrap if empty, then serve.

Railway runs this as the web process. It is deliberately defensive: a container
that dies on boot gives you a build log and nothing else, so every step here
either succeeds, or logs precisely what went wrong and keeps going far enough
to serve a page that tells you.

Order matters:

1. Normalise DATABASE_URL. Railway hands out ``postgresql://``; some older
   plugins still hand out ``postgres://``, which SQLAlchemy 2 rejects outright.
2. Run migrations before anything imports a model and caches metadata.
3. Bootstrap the last 30 days IF the database is empty. Railway's filesystem is
   ephemeral and Postgres starts bare, so without this the first deploy serves
   empty tabs and looks broken.
4. Serve on $PORT. Railway assigns it; binding anything else fails the health
   check with no useful message.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
log = logging.getLogger("start")


def normalise_database_url() -> str:
    """Return the DATABASE_URL SQLAlchemy will actually accept."""
    url = (os.environ.get("DATABASE_URL") or "").strip()

    if not url:
        log.error(
            "DATABASE_URL is not set. On Railway this means no Postgres service is "
            "attached to this app. Add one (New -> Database -> PostgreSQL), then "
            "reference it from this service's Variables as DATABASE_URL. "
            "SQLite is NOT usable on Railway: the filesystem is wiped on every "
            "redeploy and every report would silently reset to empty."
        )
        # Fall back so the container still boots and serves the explanation
        # rather than crash-looping with no page to look at.
        url = "sqlite:///data/towbook.db"
        os.environ["DATABASE_URL"] = url
        return url

    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]

    os.environ["DATABASE_URL"] = url
    scheme = url.split("://", 1)[0]
    log.info("database backend: %s", scheme)
    return url


def run_migrations() -> None:
    log.info("running alembic upgrade head")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        log.info("migrations applied")
        return

    # A fresh database with no alembic_version, created by create_all, is the
    # one case worth repairing automatically -- see core/db.py.
    log.error("alembic failed (exit %s)", result.returncode)
    for line in (result.stderr or "").strip().splitlines()[-12:]:
        log.error("  %s", line)
    log.warning("falling back to create_all so the service can still start")
    try:
        from towbook_agent.core.db import init_db

        init_db()
        log.info("schema created directly")
    except Exception:
        log.exception("could not create the schema; the board will show an error page")


def bootstrap_if_empty() -> None:
    """Pull the last 30 days on a cold database.

    The owner's scope is 'the last 30 days, and then going forward'. A cold
    Railway Postgres has neither, so the first boot fetches the window and the
    scheduler carries it from there.
    """
    if (os.environ.get("BOOTSTRAP_ON_EMPTY") or "true").lower() not in {"1", "true", "yes"}:
        log.info("bootstrap disabled by BOOTSTRAP_ON_EMPTY")
        return

    try:
        from sqlalchemy import func, select

        from towbook_agent.core.db import get_session
        from towbook_agent.core.models import Request

        with get_session() as session:
            count = session.scalar(select(func.count()).select_from(Request)) or 0
    except Exception:
        log.exception("could not check whether the database is empty; skipping bootstrap")
        return

    if count:
        log.info("database already holds %s requests; no bootstrap needed", count)
        return

    if not (os.environ.get("TOWBOOK_USER") and os.environ.get("TOWBOOK_PASS")):
        log.warning(
            "database is empty and TOWBOOK_USER / TOWBOOK_PASS are not set, so there "
            "is nothing to load. Set them in Railway variables and redeploy."
        )
        return

    days = int(os.environ.get("BOOTSTRAP_DAYS") or 30)
    log.info("cold database: loading the last %s days from Towbook", days)
    try:
        import datetime as dt

        from towbook_agent.core.scheduler import run_pipeline

        today = dt.date.today()
        loaded = 0
        for offset in range(days, -1, -1):
            day = today - dt.timedelta(days=offset)
            try:
                run_pipeline("daily", day=day, notify=False)
                loaded += 1
            except Exception as exc:  # one bad day must not stop the bootstrap
                log.warning("bootstrap: %s failed (%s)", day, exc)
        log.info("bootstrap complete: %s of %s days loaded", loaded, days + 1)
    except Exception:
        log.exception("bootstrap failed; the board will start empty and fill on schedule")


def main() -> int:
    normalise_database_url()
    run_migrations()
    bootstrap_if_empty()

    port = int(os.environ.get("PORT") or os.environ.get("DASHBOARD_PORT") or 8080)
    log.info("serving on 0.0.0.0:%s", port)

    import uvicorn

    # Single worker on purpose: the APScheduler instance lives in this process,
    # and a second worker would run every scheduled job twice.
    uvicorn.run(
        "towbook_agent.web.app:app",
        host="0.0.0.0",
        port=port,
        workers=1,
        log_level=(os.environ.get("LOG_LEVEL") or "info").lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
