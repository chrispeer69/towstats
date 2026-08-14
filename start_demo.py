"""Container entrypoint for the DEMO service: seed if needed, then serve.

Set this as the start command on a Railway service pointed at this repo, with
`TOWBOOK_REPO_ROOT` and `DATABASE_URL` aimed at the demo root. It is the demo
counterpart to `start.py`, and it delegates to that file for everything that is
not demo-specific rather than keeping a second copy of the boot sequence.

WHY THE DEMO SEEDS ITSELF ON BOOT
--------------------------------
Two problems with one answer.

**The filesystem is ephemeral.** A container's disk does not survive a
redeploy, so a SQLite demo database is wiped on every `git push`. `start.py`
says so at CRITICAL for the production service, where it means "you are about
to lose real customer data". Here it is not a warning, it is the design: the
demo owns nothing that needs to survive, because it can rebuild itself in
about a minute.

**A static demo ages.** The generated window ends at the moment it was seeded,
so a demo seeded in May is, by August, a board whose newest job is three
months old -- shown to a prospect as evidence the product watches their work
in real time. Re-seeding on boot means the demo is always current.

So the ephemeral filesystem stops being an obstacle and starts being the
refresh mechanism.

WHAT IT WILL NOT DO
-------------------
* **Seed anything that is not the demo.** The seeder refuses a DATABASE_URL
  that does not look like the demo database and one that resolves outside the
  demo root; this file additionally refuses to start without
  TOWBOOK_REPO_ROOT. Three checks, because the failure being guarded against
  is writing thousands of synthetic offers into a real customer's board.
* **Contact Towbook.** `BOOTSTRAP_ON_EMPTY` and `RUN_SCHEDULER` are forced
  off. The demo tenant has no account, and a cold-start backfill or a
  scheduled pull would be an hourly login attempt for credentials that do not
  exist.
* **Crash the service because seeding failed.** A board that comes up thin is
  worse than one that comes up full and better than one that does not come up
  at all -- the same reasoning as `start.py`'s backfill.

CONFIGURATION
-------------
| Variable                | Default | Meaning                                  |
|-------------------------|---------|------------------------------------------|
| `DEMO_SEED_ON_BOOT`     | `true`  | seed at startup at all                   |
| `DEMO_MAX_AGE_HOURS`    | `20`    | re-seed if the newest offer is older     |
| `DEMO_WEEKS`            | `13`    | whole weeks of history to generate       |
| `DEMO_FORCE_RESEED`     | `false` | re-seed every boot, however fresh        |

The default of 20 hours means a service that happens to stay up for days still
refreshes daily, while a normal redeploy (which arrives with an empty disk)
seeds because the database is empty rather than because of the clock.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
log = logging.getLogger("start_demo")

_TRUTHY = {"1", "true", "yes", "on"}

#: Must match scripts/seed_demo.py -> COMPANY_ID. Duplicated as a literal so
#: the guard below can run before the seeder is imported.
DEMO_COMPANY_ID = "example-towing"


def _flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def _int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        log.warning("%s is not a number; using %s", name, default)
        return default


def guard_environment() -> None:
    """Refuse to boot as the demo without the demo's isolation in place."""
    root = (os.environ.get("TOWBOOK_REPO_ROOT") or "").strip()
    if not root:
        raise SystemExit(
            "TOWBOOK_REPO_ROOT is not set. start_demo.py serves the DEMO tenant "
            "and must be pointed at the demo root (e.g. /app/demo-root), which "
            "is what keeps its roster and its database separate from the "
            "production ones. Use start.py for the production service."
        )
    roster = Path(root) / "config" / "companies.yaml"
    if not roster.is_file():
        raise SystemExit(
            f"TOWBOOK_REPO_ROOT is {root!r}, which has no config/companies.yaml. "
            f"That is not the demo root."
        )

    # "It has a roster" is not enough -- the PRODUCTION root has one too, and
    # pointing this entrypoint at it would boot the demo seeder against the
    # live tenants. The roster must actually name the demo tenant as its
    # default before this file will treat the root as the demo's.
    from towbook_agent.core.companies import default_company_id

    try:
        default = default_company_id()
    except Exception as exc:  # unreadable roster
        raise SystemExit(f"could not read the roster at {roster}: {exc}") from None

    if default != DEMO_COMPANY_ID:
        raise SystemExit(
            f"TOWBOOK_REPO_ROOT is {root!r}, whose roster opens on "
            f"{default!r}, not {DEMO_COMPANY_ID!r}. That is not the demo root -- "
            f"it looks like a production one. Use start.py for that service."
        )

    # The demo must never reach Towbook: it has no account to reach it with.
    os.environ["RUN_SCHEDULER"] = "false"
    os.environ["BOOTSTRAP_ON_EMPTY"] = "false"


def newest_offer_age() -> timedelta | None:
    """How old the most recent stored offer is, or None if there are none."""
    from datetime import datetime, timezone

    try:
        from sqlalchemy import func, select

        from towbook_agent.core.db import get_session
        from towbook_agent.core.models import Request

        with get_session(commit=False) as session:
            newest = session.execute(select(func.max(Request.offered_at))).scalar_one_or_none()
    except Exception:
        log.exception("could not read the newest stored offer; assuming the demo is cold")
        return None

    if newest is None:
        return None

    # offered_at is stored in UTC. It comes back naive on SQLite and aware on
    # Postgres, so the reference clock has to match rather than assume either.
    if newest.tzinfo is None:
        return datetime.now(timezone.utc).replace(tzinfo=None) - newest
    return datetime.now(timezone.utc) - newest


def seed_if_needed() -> None:
    """Rebuild the demo data when it is missing or stale. Never raises."""
    if not _flag("DEMO_SEED_ON_BOOT", True):
        log.info("DEMO_SEED_ON_BOOT is false; serving whatever is already stored")
        return

    force = _flag("DEMO_FORCE_RESEED", False)
    max_age = timedelta(hours=_int("DEMO_MAX_AGE_HOURS", 20))

    age = newest_offer_age()
    if force:
        reason = "DEMO_FORCE_RESEED is set"
    elif age is None:
        reason = "the demo database holds no offers"
    elif age > max_age:
        reason = f"the newest offer is {age.total_seconds() / 3600:.1f}h old"
    else:
        log.info(
            "demo data is %.1fh old, within DEMO_MAX_AGE_HOURS; not re-seeding",
            age.total_seconds() / 3600,
        )
        return

    log.info("seeding the demo because %s", reason)

    argv = sys.argv
    try:
        import seed_demo

        # --reset only matters when something is already stored; passing it on a
        # cold database is a no-op that prints one line.
        sys.argv = [
            "seed_demo.py",
            "--weeks",
            str(_int("DEMO_WEEKS", 13)),
            "--reset",
        ]
        code = seed_demo.main()
        if code == 0:
            log.info("demo seeded")
        else:
            log.error(
                "the demo seeder exited %s. The board will serve whatever is "
                "stored, which may be nothing.",
                code,
            )
    except SystemExit as exc:  # the seeder's own refusals
        log.error("the demo seeder refused to run: %s", exc)
    except Exception:
        log.exception("seeding the demo failed; serving whatever is stored")
    finally:
        sys.argv = argv


def main() -> int:
    guard_environment()

    import start

    start.check_storage()
    start.migrate()
    seed_if_needed()

    import uvicorn

    from towbook_agent.web.app import resolve_host, resolve_port

    host = resolve_host("0.0.0.0")
    port = resolve_port()
    log.info("serving the demo on %s:%s", host, port)

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
