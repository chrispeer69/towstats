"""cancellation_review -- the standing deep-dive on cancelled work.

WHY A SECOND REPORT
    The morning report answers "how did yesterday go". This answers "why do we
    keep losing work to cancellations, and what do we change". It is re-run on
    demand (weekly is the natural cadence) and its whole job is to be acted on,
    so it ends in named actions rather than tables.

THE FINDING IT WAS BUILT AROUND
    "Cancelled" is not one thing. It is at least three, and they have nothing
    in common but the label:

      1. UNANSWERED. The offer timed out before anyone on our side touched it.
         Allstate codes these as "Cancelled" -- it has issued exactly ONE
         "expired" in 965 offers, where Agero issued 810. So Allstate's
         cancellations are mostly the thing every other client calls an
         expiry, and reading them as "the client changed its mind" hides a
         pure coverage problem.
      2. WE FAILED IT. Taken, crewed, not delivered -- "Service Failure
         Confirmed", every one of which had a person assigned.
      3. GENUINELY GONE. Gone-on-arrival, customer stood down.

    Only the first two are ours, and they have opposite fixes: (1) is who is
    watching the screen at 9 PM, (2) is dispatch discipline. Reporting one
    number for both is why neither got worked.

RESPONSE WINDOWS ARE THE CONSTRAINT
    Median seconds from offer to expiry, measured: Allstate 1.8 minutes,
    NSD 2.0, Agero 3.0 (p90 7.0). Allstate's window is the tightest and its
    volume is heaviest in the hours we cover worst. That is the whole story.

Counts are jobs. This account reports no offer amounts.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Inches, Pt

from towbook_agent.agents.morning_report import (
    CANCEL_KINDS,
    CANCEL_OTHER,
    INK,
    LOSS_C,
    MUTED,
    RULE,
    WIN_C,
    _border,
    _cancel_kind,
    _pct,
    _section,
    _shade,
    _text,
)

#: A client needs this many offers before its coding style is worth naming.
MIN_CLIENT_OFFERS = 40

#: An hour needs this many offers before it is called out as a coverage gap.
MIN_HOUR_OFFERS = 15

#: Untouched share above which an hour is treated as uncovered rather than busy.
UNCOVERED_THRESHOLD = 0.45


@dataclass
class ClientProfile:
    """One client's outcome mix and how it labels dead offers."""

    name: str
    offers: int = 0
    accepted: int = 0
    denied: int = 0
    expired: int = 0
    canceled: int = 0
    untouched_cancels: int = 0
    windows: list[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float | None:
        return (self.accepted / self.offers) if self.offers else None

    @property
    def cancel_share(self) -> float | None:
        return (self.canceled / self.offers) if self.offers else None

    @property
    def median_window(self) -> float | None:
        return statistics.median(self.windows) if self.windows else None

    @property
    def codes_timeouts_as_cancelled(self) -> bool:
        """True when a client has cancellations but essentially never expires.

        This is the tell. A client with hundreds of dead offers and no
        "expired" is not describing client behaviour, it is using a different
        word for the same event -- and its cancellations must be read as
        unanswered work.
        """
        dead = self.expired + self.canceled
        return dead >= 20 and self.canceled > 0 and (self.expired / dead) < 0.05


@dataclass
class Review:
    start: date
    end: date
    clients: dict[str, ClientProfile] = field(default_factory=dict)
    kinds: Counter = field(default_factory=Counter)
    kinds_engaged: Counter = field(default_factory=Counter)
    reasons: Counter = field(default_factory=Counter)
    #: local hour -> [offers, untouched cancellations] for the focus client
    focus_hours: dict[int, list[int]] = field(default_factory=dict)
    focus_client: str | None = None
    daily: dict[date, list[int]] = field(default_factory=dict)  # day -> [offers, cancels, untouched]

    @property
    def total_offers(self) -> int:
        return sum(c.offers for c in self.clients.values())

    @property
    def total_cancels(self) -> int:
        return sum(c.canceled for c in self.clients.values())

    @property
    def total_untouched(self) -> int:
        return sum(c.untouched_cancels for c in self.clients.values())

    @property
    def controllable(self) -> int:
        """Cancellations inside our control: unanswered plus our own failures."""
        ours = sum(self.kinds[label] for _raw, label, blame in CANCEL_KINDS if blame)
        return self.total_untouched + ours


def _bounds(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


def load_review(conn: sqlite3.Connection, start: date, end: date, tz: ZoneInfo) -> Review:
    """Aggregate every offer in the local window [start, end]."""
    review = Review(start=start, end=end)
    lo, hi = _bounds(start, tz), _bounds(end + timedelta(days=1), tz)

    rows = conn.execute(
        """
        SELECT client_name, status, status_raw, driver_assigned, denial_reason,
               offered_at, expires_at
          FROM requests
         WHERE offered_at >= ? AND offered_at < ?
        """,
        (lo.isoformat(sep=" "), hi.isoformat(sep=" ")),
    ).fetchall()

    for name, status, raw, driver, reason, offered_at, expires_at in rows:
        client = review.clients.setdefault(name or "Unknown", ClientProfile(name or "Unknown"))
        client.offers += 1

        stamp = datetime.fromisoformat(str(offered_at).replace("Z", ""))
        local = stamp.replace(tzinfo=timezone.utc).astimezone(tz)
        day = local.date()
        bucket = review.daily.setdefault(day, [0, 0, 0])
        bucket[0] += 1

        if expires_at:
            try:
                exp = datetime.fromisoformat(str(expires_at).replace("Z", ""))
                minutes = (exp - stamp).total_seconds() / 60
                if 0 < minutes < 120:  # guard against clock junk
                    client.windows.append(minutes)
            except ValueError:
                pass

        if status == "accepted":
            client.accepted += 1
        elif status == "denied":
            client.denied += 1
        elif status == "expired":
            client.expired += 1
        elif status == "canceled":
            client.canceled += 1
            bucket[1] += 1
            kind = _cancel_kind(raw)
            review.kinds[kind] += 1
            if driver:
                review.kinds_engaged[kind] += 1
            else:
                client.untouched_cancels += 1
                bucket[2] += 1
            if reason:
                review.reasons[reason.strip()] += 1

    # The focus client is the one losing the most work to untouched cancels --
    # chosen from the data rather than hard-coded, so the report keeps pointing
    # at the real problem after this one is fixed.
    if review.clients:
        focus = max(review.clients.values(), key=lambda c: c.untouched_cancels)
        if focus.untouched_cancels:
            review.focus_client = focus.name
            hours: dict[int, list[int]] = defaultdict(lambda: [0, 0])
            for name, status, raw, driver, _reason, offered_at, _exp in rows:
                if name != focus.name:
                    continue
                stamp = datetime.fromisoformat(str(offered_at).replace("Z", ""))
                h = stamp.replace(tzinfo=timezone.utc).astimezone(tz).hour
                hours[h][0] += 1
                if status == "canceled" and not driver:
                    hours[h][1] += 1
            review.focus_hours = dict(hours)
    return review
