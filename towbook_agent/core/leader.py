"""One scheduler, however many web processes.

The board is now the only delivery mechanism, so the scheduler runs *inside* the
web process (see :func:`towbook_agent.core.scheduler.start_background_scheduler`).
That trades one failure mode for another: a web service scaled to two workers or
two replicas would run two schedulers, and two schedulers means the hourly job
fires twice.

Firing twice is not a data-corruption bug -- every job in this system is
idempotent by construction (hard constraint #4): the window comes from the clock,
``run_id`` is derived from the window, requests upsert on ``request_id`` and each
metrics table has a unique key on its window column. Two runs of the 14:00 job
produce exactly the 14:00 numbers, once. What it *is* is two full portal pulls an
hour, doubled API load against Towbook for no benefit, and two racing writers on
the same rows.

So the guard is defence in depth, deliberately, in this order:

1. **One web worker.** The shipped ``Procfile`` and ``railway.json`` start
   uvicorn with no ``--workers`` flag, which is one worker, and the README says
   not to add one. This is the primary guard because it is the only one that
   works on every backend including SQLite, and because it needs nothing from
   the database to be correct.
2. **A PostgreSQL advisory lock.** The primary guard is a promise about how the
   process is launched, and a promise like that survives until the first time
   somebody scales the service to two replicas from the Railway UI -- which
   takes one click and produces no error. ``pg_try_advisory_lock`` is the
   enforcement: the second process asks for the same lock, is told no, and comes
   up as a read-only board. It costs one held connection and no polling.

On SQLite there is no lock and none is needed: SQLite means a single machine and
a developer who launched the process on purpose, and the honest answer to "is
another scheduler running" there is "you would know".

The lock is *session* scoped (``pg_try_advisory_lock``, not the ``_xact_``
variant), so it is held for as long as the connection is, and it is released
automatically if the process dies.

THAT IS NOT ENOUGH ON ITS OWN, and this file used to claim it was. The original
note here said the automatic release "is what makes a redeploy hand the
scheduler over to the new container without any manual cleanup". It does not,
because a zero-downtime platform starts the replacement container *before* it
stops the old one. During that overlap the old container still holds the lock
and the new one is refused -- correctly. The bug was that being refused was
treated as final: the new container logged "expected on a second replica", ran
as a board-only process, and after the old container exited there was no
scheduler left anywhere. Observed in production as 5.7 hours with no hourly
run, whose only symptom was the board's own overdue banner.

Asking once is not leader election. The retry loop that makes it one lives in
:func:`towbook_agent.core.scheduler._watch_for_the_lease`: a process that is
refused keeps asking, and takes over when the holder goes away. Everything in
this module is still a single, honest attempt -- it just is not the last one.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any

from . import db as core_db

__all__ = [
    "SCHEDULER_LOCK_NAME",
    "advisory_lock_key",
    "SchedulerLease",
    "acquire_scheduler_lease",
]

logger = logging.getLogger(__name__)

#: Namespaced so a second application sharing the database cannot collide with
#: it. Change it only if you intend two independent schedulers on one database.
SCHEDULER_LOCK_NAME: str = "towbook-agent:scheduler"

#: Postgres advisory lock keys are signed 64-bit integers.
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def advisory_lock_key(name: str = SCHEDULER_LOCK_NAME) -> int:
    """Hash a name to a stable signed 64-bit advisory lock key.

    Deterministic across processes, restarts and versions -- two containers
    running the same release must compute the same number or the lock guards
    nothing. ``LOCK_NAMESPACE`` lets several deployments share one Postgres
    instance without fighting over the same key.
    """
    namespace = (os.environ.get("LOCK_NAMESPACE") or "").strip()
    digest = hashlib.sha256(f"{namespace}{name}".encode("utf-8")).digest()[:8]
    value = int.from_bytes(digest, "big", signed=True)
    return max(_INT64_MIN, min(_INT64_MAX, value))


@dataclass
class SchedulerLease:
    """The outcome of asking "may I be the scheduler in this deployment?".

    ``acquired`` False is a normal, healthy state for a second replica: it serves
    the board and lets the leader do the work. It is logged at WARNING rather
    than ERROR for exactly that reason.
    """

    acquired: bool
    backend: str
    reason: str
    key: int | None = None
    _connection: Any = None

    def release(self) -> None:
        """Give the lock back. Idempotent, and never raises.

        Called on clean shutdown. An unclean exit needs no cleanup: Postgres
        drops a session-scoped advisory lock when the connection goes away.
        """
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            from sqlalchemy import text

            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self.key})
            connection.commit()
        except Exception as exc:  # pragma: no cover - depends on the environment
            logger.debug("could not release the scheduler advisory lock: %s", exc)
        finally:
            try:
                connection.close()
            except Exception:  # pragma: no cover
                pass
            logger.info("released the scheduler advisory lock")


def acquire_scheduler_lease(name: str = SCHEDULER_LOCK_NAME) -> SchedulerLease:
    """Try to become the one process that runs scheduled jobs.

    **Never raises.** A database that cannot answer the question must not stop
    the board from serving; the scheduler is allowed to start in that case,
    because a stale board is the failure this whole design exists to prevent and
    a duplicated idempotent run is not.
    """
    backend = core_db.backend_name()

    if not backend.startswith("postgres"):
        return SchedulerLease(
            acquired=True,
            backend=backend or "unknown",
            reason=(
                "no advisory lock on this backend; a single uvicorn worker is the guard "
                "(see towbook_agent/core/leader.py)"
            ),
        )

    key = advisory_lock_key(name)
    connection = None
    try:
        from sqlalchemy import text

        engine = core_db.get_engine()
        # A dedicated connection, checked out of the pool and never returned:
        # a session-scoped advisory lock lives exactly as long as its session.
        connection = engine.connect()

        # MAKE THE SERVER NOTICE A DEAD HOLDER.
        #
        # "Released automatically if the process dies" is only true once
        # PostgreSQL realises the client is gone, and a container that is killed
        # never closes its socket. The server's default keepalive comes from the
        # OS -- typically two hours -- so the backend sits `idle`, holding this
        # lock, long after the container that took it stopped existing. Observed
        # in production: a free-looking deployment with no scheduler, because an
        # orphan from the previous deploy still owned the lock.
        #
        # These are per-session settings of the SERVER's keepalive toward this
        # client, so a holder that vanishes is detected in about a minute
        # (30s idle + 3 x 10s probes) and its lock released. Wrapped
        # individually: they are an optimisation, and a managed Postgres that
        # disallows them must not cost us the lock entirely.
        for statement in (
            "SET tcp_keepalives_idle = 30",
            "SET tcp_keepalives_interval = 10",
            "SET tcp_keepalives_count = 3",
        ):
            try:
                connection.execute(text(statement))
            except Exception as exc:  # pragma: no cover - depends on the server
                logger.debug("could not apply %r: %s", statement, exc)

        granted = bool(
            connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
        )
        connection.commit()
    except Exception as exc:
        logger.error(
            "could not take the scheduler advisory lock (%s: %s); starting the scheduler "
            "anyway, because a board that stops updating is the worse failure. Jobs are "
            "idempotent, so a duplicate run recomputes the same window.",
            type(exc).__name__,
            exc,
        )
        if connection is not None:
            try:
                connection.close()
            except Exception:  # pragma: no cover
                pass
        return SchedulerLease(
            acquired=True, backend=backend, reason=f"lock unavailable: {type(exc).__name__}", key=key
        )

    if not granted:
        try:
            connection.close()
        except Exception:  # pragma: no cover
            pass
        logger.warning(
            "another process already holds the scheduler advisory lock (key %d); this "
            "process serves the board only for now and will keep asking for the lock. "
            "Expected on a second replica, and expected for a few seconds during a "
            "deploy while the container being replaced is still running.",
            key,
        )
        return SchedulerLease(
            acquired=False, backend=backend, reason="another process is the scheduler", key=key
        )

    logger.info("holding the scheduler advisory lock (key %d)", key)
    return SchedulerLease(
        acquired=True,
        backend=backend,
        reason="advisory lock held",
        key=key,
        _connection=connection,
    )
