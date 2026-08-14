"""Container entrypoint: check the storage, migrate, backfill once, then serve.

Railway (and Render, and Fly, and Heroku) run this as the web process. It is
deliberately defensive: a container that dies on boot leaves you a build log and
nothing else, so every step below either succeeds or logs exactly what went
wrong and keeps going far enough to serve a page that says so. The board is the
only delivery mechanism -- there is no SMS and no email to fall back on -- so
"came up and displayed the error" beats "crash-looped" every time.

This file is a THIN WRAPPER. Every decision it needs is already implemented in
the package, and it calls that code rather than reimplementing it, because a
second copy of the boot logic is a second copy that can be wrong:

* URL normalisation      -> towbook_agent.core.db.normalize_database_url
* the ephemeral warning  -> towbook_agent.core.db.warn_if_ephemeral_sqlite
* alembic upgrade head   -> towbook_agent.core.db.upgrade_to_head
* host / port resolution -> towbook_agent.web.app.resolve_host / resolve_port
* the scheduler          -> started by the app's own lifespan hook, once it is
                            serving, with a Postgres advisory lock so two
                            replicas cannot both schedule

Running ``python -m towbook_agent serve`` directly does the same thing minus the
cold-start backfill, which is the only behaviour unique to this file.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
log = logging.getLogger("start")

_TRUTHY = {"1", "true", "yes", "on"}


def check_storage() -> None:
    """Refuse to be quiet about a database that is about to be deleted.

    A container filesystem is ephemeral. ``sqlite:///data/towbook.db`` is the
    default, so a service with no DATABASE_URL boots perfectly, serves real
    numbers all week, and wipes every one of them on the next ``git push``. It
    is not an error the process can see -- so it is said here, at CRITICAL, and
    again on a red banner across every tab of the board.
    """
    from towbook_agent.core.db import database_url, warn_if_ephemeral_sqlite

    raw = (os.environ.get("DATABASE_URL") or "").strip()
    if not raw:
        log.critical(
            "DATABASE_URL is not set. On Railway that means no Postgres service is "
            "attached: add one (New -> Database -> PostgreSQL), then reference it from "
            "this service's Variables as DATABASE_URL=${{Postgres.DATABASE_URL}}. "
            "SQLite is NOT usable here -- the filesystem is wiped on every redeploy and "
            "the whole history silently resets to empty."
        )
    else:
        warn_if_ephemeral_sqlite()

    # database_url() applies the postgres:// and driver rewrites. Logged with the
    # password removed, because this line ends up in a build log.
    url = database_url()
    log.info("database backend: %s", url.split("://", 1)[0])


def migrate() -> bool:
    """``alembic upgrade head``, in-process. Idempotent; never raises.

    In-process rather than ``subprocess alembic upgrade head`` so it resolves the
    connection string through exactly the code the application uses, and so the
    "tables exist but were never stamped" repair in
    :func:`towbook_agent.core.db.upgrade_to_head` applies here too. A
    subprocess would have to rediscover both.
    """
    from towbook_agent.core.db import upgrade_to_head

    result = upgrade_to_head()
    if result["ok"]:
        log.info("database at revision %s (%s)", result["revision"], result["action"])
        return True
    log.critical("MIGRATION FAILED: %s", result["error"])
    log.critical("the board will start, show a banner, and be wrong until this is fixed")
    return False


def backfill_if_empty() -> None:
    """On a cold database, load the trailing window once so the board is not blank.

    A fresh Postgres has no history, and the scheduler only ever computes the
    window that just closed -- so without this the first deploy shows empty tabs
    for a day and looks broken rather than new.

    ONE acquisition, then metrics-only recomputes. The API call is paged over the
    whole window in a single pull; each day's metrics are then computed from rows
    already in the database with ``skip_ingest=True``, which touches no network.
    Doing it as N daily pipeline runs would mean N logins and N pulls, would take
    long enough to fail the platform's health check, and would hammer Towbook on
    every cold start.

    Off with ``BOOTSTRAP_ON_EMPTY=false``. Never raises: a failed backfill leaves
    an empty board that fills on the next scheduled run, which is a delay, not a
    fault.
    """
    if (os.environ.get("BOOTSTRAP_ON_EMPTY") or "true").strip().lower() not in _TRUTHY:
        log.info("cold-start backfill disabled by BOOTSTRAP_ON_EMPTY")
        return

    try:
        from sqlalchemy import func, select

        from towbook_agent.core.db import get_session
        from towbook_agent.core.models import Request

        with get_session(commit=False) as session:
            count = int(session.execute(select(func.count()).select_from(Request)).scalar_one())
    except Exception:
        log.exception("could not check whether the database is empty; skipping the backfill")
        return

    if count:
        log.info("database already holds %s request(s); no backfill needed", f"{count:,}")
        return

    if not (os.environ.get("TOWBOOK_USER") and os.environ.get("TOWBOOK_PASS")):
        log.warning(
            "the database is empty and TOWBOOK_USER / TOWBOOK_PASS are not set, so there "
            "is nothing to load. Set them in the service's variables and redeploy."
        )
        return

    try:
        days = max(1, min(int(os.environ.get("BOOTSTRAP_DAYS") or 30), 365))
    except (TypeError, ValueError):
        days = 30

    log.info("cold database: loading the last %d days from Towbook", days)
    try:
        from datetime import timedelta

        from towbook_agent.core.scheduler import (
            _load_agent,
            make_run_id,
            now_local,
            run_pipeline,
        )

        # Local midnight tomorrow, so the window ends after today's offers and
        # the half-open [start, end) convention still holds.
        end = now_local().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        start = end - timedelta(days=days)

        # 1. One pull, one ingest, for the whole window.
        acquisition = _load_agent("acquisition_api")
        payload = acquisition.acquire_api(start, end)
        run_id = make_run_id("backfill", "default", start)
        ingestion = _load_agent("ingestion")
        result = ingestion.ingest(payload, run_id)
        log.info("backfill ingested %s row(s)", getattr(result, "rows_upserted", "?"))

        # 2. Metrics per day, recomputed from what is now stored. No network, no
        #    notifications -- notifications are off anyway, but say so explicitly
        #    so re-enabling a channel later does not send 30 backdated reports.
        computed = 0
        day = start
        while day < end:
            outcome = run_pipeline(
                "daily",
                day,
                day + timedelta(days=1),
                skip_ingest=True,
                notify=False,
            )
            computed += 1 if outcome.ok else 0
            day += timedelta(days=1)
        log.info("backfill complete: %d of %d day(s) computed", computed, days)
    except Exception:
        log.exception("backfill failed; the board starts empty and fills on the next run")


#: The demo tenant's id. Must match scripts/seed_demo.py -> COMPANY_ID.
_DEMO_COMPANY_ID = "example-towing"


def is_demo_deployment() -> bool:
    """Whether this process was pointed at the demo roster rather than a real one.

    WHY THIS DISPATCH EXISTS. `railway.json` and `Procfile` name ONE start
    command for the whole repository, and the demo needs a different boot
    sequence -- it seeds itself before serving (see start_demo.py). Railway can
    override the start command per service in its dashboard, but that puts a
    load-bearing setting somewhere the repository cannot see, where it is
    invisible in review and lost on a service rebuild. Deciding from the roster
    keeps the whole thing in git.

    It reads the roster rather than a flag, because the roster is the thing
    that actually makes a deployment the demo. A `DEMO=true` variable could be
    set on a production service by accident; a production roster opening on
    `example-towing` is not a mistake anyone can make by mistyping a variable.

    Production cannot trigger this: its roster opens on `default`.
    """
    if not (os.environ.get("TOWBOOK_REPO_ROOT") or "").strip():
        return False
    try:
        from towbook_agent.core.companies import default_company_id

        return default_company_id() == _DEMO_COMPANY_ID
    except Exception:
        # An unreadable roster is not evidence of a demo. Fall through to the
        # production path, which reports the problem properly.
        return False


def main() -> int:
    if is_demo_deployment():
        log.info(
            "TOWBOOK_REPO_ROOT names the demo roster (default company %r); "
            "handing off to start_demo.py",
            _DEMO_COMPANY_ID,
        )
        import start_demo

        return start_demo.main()

    check_storage()
    migrate()
    backfill_if_empty()

    import uvicorn

    from towbook_agent.web.app import resolve_host, resolve_port

    host = resolve_host("0.0.0.0")
    port = resolve_port()
    log.info("serving on %s:%s", host, port)

    # ONE WORKER. The scheduler runs inside this process (see the lifespan hook
    # in towbook_agent/web/app.py), so N workers would be N schedulers pulling
    # the Towbook API on the same cron. core/leader.py's advisory lock catches
    # that on Postgres; one worker is the configuration where it cannot happen.
    #
    # proxy_headers/forwarded_allow_ips: the platform terminates TLS and proxies,
    # so without these every request looks like it came from the proxy over
    # plain HTTP and the login cookie's Secure flag misbehaves behind it.
    uvicorn.run(
        "towbook_agent.web.app:app",
        host=host,
        port=port,
        workers=1,
        log_level=(os.environ.get("LOG_LEVEL") or "info").lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
