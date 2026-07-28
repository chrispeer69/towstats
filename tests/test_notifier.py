"""Suppression rules, and the one thing that is never suppressed.

Quiet hours and the rate limit exist so the owner's phone is not a nuisance at
03:00. They are also the most dangerous code in the system, because a
suppression bug is invisible -- nobody notices the alert that never arrived.

So the tests here are mostly about what must still get through:

* ``pipeline_failure`` ignores quiet hours **and** the rate limit. A missing
  report has to be louder than a bad one; silence is never treated as success.
* ``high`` severity is deliberately absent from ``suppress_severities``.

Nothing here can send anything: the Twilio and SMTP layers are replaced with
recorders, and ``no_network`` in conftest makes a real connection raise.
"""

from __future__ import annotations

import smtplib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from towbook_agent.core.config_loader import get_notifications
from towbook_agent.core.db import get_session
from towbook_agent.core.models import AlertFired

# --------------------------------------------------------------------------
# Recording every way a message could leave the process
# --------------------------------------------------------------------------


@dataclass
class SendCapture:
    """Whatever the notifier tried to send, and through which door."""

    sms: list[dict[str, Any]] = field(default_factory=list)
    email: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.sms) + len(self.email)

    def bodies(self) -> list[str]:
        return [str(item.get("body", "")) for item in self.sms + self.email]

    def clear(self) -> None:
        self.sms.clear()
        self.email.clear()

    def describe(self) -> str:
        return f"sms={self.sms!r} email={self.email!r}"


#: Function names the notifier might expose for its own send step. Patching at
#: this level is preferred because it records the rendered body; the transport
#: level below is the safety net for a notifier that does not use these names.
_SMS_HOOKS = ("send_sms", "_send_sms", "deliver_sms", "_deliver_sms")
_EMAIL_HOOKS = ("send_email", "_send_email", "deliver_email", "_deliver_email")


@pytest.fixture
def capture(notifier, monkeypatch: pytest.MonkeyPatch) -> SendCapture:
    """Replace every outbound path with a recorder and configure fake creds."""
    recorder = SendCapture()

    # Credentials have to look present or a sane notifier short-circuits before
    # it ever consults the suppression rules. They are fake and unusable.
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest00000000000000000000000000")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token-not-real")
    monkeypatch.setenv("TWILIO_FROM", "+15550000000")
    monkeypatch.setenv("SMTP_HOST", "smtp.invalid")
    monkeypatch.setenv("SMTP_PORT", "25")
    monkeypatch.setenv("SMTP_USER", "smtp-user")
    monkeypatch.setenv("SMTP_PASS", "smtp-pass-not-real")
    monkeypatch.setenv("SMTP_FROM", "towbook@example.invalid")

    def record_sms(*args: Any, **kwargs: Any) -> Any:
        recorder.sms.append(_normalise(args, kwargs))
        return {"sid": f"SM{len(recorder.sms):032d}"}

    def record_email(*args: Any, **kwargs: Any) -> Any:
        recorder.email.append(_normalise(args, kwargs))
        return True

    for name in _SMS_HOOKS:
        if callable(getattr(notifier, name, None)):
            monkeypatch.setattr(notifier, name, record_sms)
    for name in _EMAIL_HOOKS:
        if callable(getattr(notifier, name, None)):
            monkeypatch.setattr(notifier, name, record_email)

    _patch_twilio(monkeypatch, notifier, record_sms)
    _patch_smtp(monkeypatch, notifier, record_email)

    return recorder


def _normalise(args: tuple, kwargs: dict) -> dict[str, Any]:
    """Flatten a send call into ``{to, body, ...}`` however it was invoked."""
    item: dict[str, Any] = dict(kwargs)
    positional = [arg for arg in args if isinstance(arg, str)]
    if "to" not in item and positional:
        item["to"] = positional[0]
    if "body" not in item and len(positional) > 1:
        item["body"] = positional[1]
    for alias in ("message", "text", "content"):
        if "body" not in item and alias in kwargs:
            item["body"] = kwargs[alias]
    item.setdefault("args", args)
    return item


def _patch_twilio(monkeypatch: pytest.MonkeyPatch, notifier: Any, record: Any) -> None:
    """Replace twilio.rest.Client wherever the notifier can reach it."""

    class FakeMessages:
        def create(self, **kwargs: Any) -> Any:
            record(**kwargs)
            return type("Message", (), {"sid": "SMtest", "status": "queued"})()

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.messages = FakeMessages()

    try:
        import twilio.rest as twilio_rest
    except Exception:  # pragma: no cover - twilio always installed here
        return
    monkeypatch.setattr(twilio_rest, "Client", FakeClient, raising=False)
    if "twilio" in sys.modules:
        monkeypatch.setattr(sys.modules["twilio"].rest, "Client", FakeClient, raising=False)
    if getattr(notifier, "Client", None) is not None:
        monkeypatch.setattr(notifier, "Client", FakeClient, raising=False)


def _patch_smtp(monkeypatch: pytest.MonkeyPatch, notifier: Any, record: Any) -> None:
    class FakeSMTP:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "FakeSMTP":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def starttls(self, *args: Any, **kwargs: Any) -> None:
            return None

        def login(self, *args: Any, **kwargs: Any) -> None:
            return None

        def ehlo(self, *args: Any, **kwargs: Any) -> None:
            return None

        def send_message(self, message: Any, *args: Any, **kwargs: Any) -> dict:
            record(to=str(message.get("To", "")), body=str(message), subject=message.get("Subject"))
            return {}

        def sendmail(self, sender: str, to: Any, body: str, *args: Any, **kwargs: Any) -> dict:
            record(to=to, body=body)
            return {}

        def quit(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    for name in ("SMTP", "SMTP_SSL"):
        if getattr(notifier, name, None) is not None:
            monkeypatch.setattr(notifier, name, FakeSMTP, raising=False)


# --------------------------------------------------------------------------
# Config the tests drive the notifier with
# --------------------------------------------------------------------------


def _notifications(
    *,
    quiet_start: str,
    quiet_end: str,
    suppress: list[str] | None = None,
    rate_limit: str = "4h",
) -> dict[str, Any]:
    """The shipped notifications.yaml with routes widened to every severity.

    The shipped routes only carry ``high`` alerts, which would make "low is
    suppressed" unobservable -- it was never going anywhere. Routing is data,
    so the test supplies its own.
    """
    base = dict(get_notifications())
    base["routes"] = [
        {"event": "alert", "channel": "sms", "to": ["owner"], "immediate": True},
        {"event": "pipeline_failure", "channel": "sms", "to": ["owner"], "immediate": True},
        {"report": "hourly", "channel": "sms", "to": ["owner"], "template": "hourly_short"},
    ]
    base["quiet_hours"] = {
        "start": quiet_start,
        "end": quiet_end,
        "suppress_severities": suppress if suppress is not None else ["low", "medium"],
    }
    base["rate_limit"] = {"same_alert_same_entity": rate_limit}
    base["non_suppressible_events"] = ["pipeline_failure"]
    return base


def _now_local() -> datetime:
    """Now, in the timezone the suite runs in (TZ=UTC, set by conftest)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hhmm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def window_containing(moment: datetime) -> tuple[str, str]:
    """A quiet-hours window that contains ``moment`` on any clock.

    Deliberately the whole day. Whether the notifier reads the wall clock in
    UTC or in the TZ timezone is its own business; these tests are about
    suppression, not about which timezone the comparison happens in, so the
    window is made immune to the difference.
    """
    return "00:00", "23:59"


def window_excluding(moment: datetime) -> tuple[str, str]:
    """A quiet-hours window on the far side of the clock from ``moment``.

    Six hours wide and centred roughly twelve hours away, so it still excludes
    ``moment`` even if the notifier's clock is up to eight hours off this one.
    """
    return _hhmm(moment + timedelta(hours=8)), _hhmm(moment + timedelta(hours=14))


def alert_payload(
    alert_id: str = "missed_tow",
    severity: str = "medium",
    entity: str = "agero",
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "alert_id": alert_id,
        "severity": severity,
        "entity": entity,
        "detail": "3 tows declined in the last hour",
        "event_type": "alert",
    }
    payload.update(extra)
    return payload


def failure_payload(**extra: Any) -> dict[str, Any]:
    payload = {
        "stage": "acquisition",
        "error": "TimeoutError: navigation to Request Log timed out",
        "severity": "high",
        "run_id": "run-123",
        "window_start": "2026-07-20T14:00",
        "window_end": "2026-07-20T15:00",
        "event_type": "pipeline_failure",
    }
    payload.update(extra)
    return payload


def freeze_clock(monkeypatch: pytest.MonkeyPatch, module: Any, when: datetime) -> None:
    """Pin the module's idea of "now", or skip with a precise reason."""
    aware = when if when.tzinfo else when.replace(tzinfo=timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return aware.astimezone(tz) if tz else aware.replace(tzinfo=None)

        @classmethod
        def utcnow(cls) -> datetime:  # type: ignore[override]
            return aware.replace(tzinfo=None)

        @classmethod
        def today(cls) -> datetime:  # type: ignore[override]
            return aware.replace(tzinfo=None)

    patched = False
    candidate = getattr(module, "datetime", None)
    if isinstance(candidate, type) and issubclass(candidate, datetime):
        monkeypatch.setattr(module, "datetime", FrozenDatetime)
        patched = True
    for name in ("_now", "now", "local_now", "utcnow", "_utcnow", "current_time"):
        if callable(getattr(module, name, None)):
            monkeypatch.setattr(module, name, lambda *a, **k: aware.replace(tzinfo=None))
            patched = True

    if not patched:
        pytest.skip(
            "agents/notifier.py exposes no clock to freeze; it should import "
            "`from datetime import datetime` or expose a `_now()` helper so "
            "quiet-hours behaviour can be tested at a chosen time"
        )


def alerts_fired() -> list[AlertFired]:
    from sqlalchemy import select

    with get_session(commit=False) as session:
        return list(session.execute(select(AlertFired).order_by(AlertFired.id)).scalars())


# --------------------------------------------------------------------------
# Quiet hours
# --------------------------------------------------------------------------


@pytest.mark.parametrize("severity", ["low", "medium"])
def test_quiet_hours_suppress_low_and_medium(
    notifier, capture, write_config, severity: str
) -> None:
    now = _now_local()
    start, end = window_containing(now)
    write_config("notifications", _notifications(quiet_start=start, quiet_end=end))

    notifier.dispatch_event("alert", alert_payload(severity=severity))

    assert capture.total == 0, (
        f"a {severity} alert was delivered inside quiet hours {start}-{end}: {capture.describe()}"
    )


def test_quiet_hours_do_not_suppress_high(notifier, capture, write_config) -> None:
    """`high` is absent from suppress_severities on purpose."""
    now = _now_local()
    start, end = window_containing(now)
    write_config("notifications", _notifications(quiet_start=start, quiet_end=end))

    notifier.dispatch_event("alert", alert_payload(alert_id="client_acceptance_drop", severity="high"))

    assert capture.total >= 1, f"a high alert was suppressed by quiet hours: {capture.describe()}"


@pytest.mark.parametrize("severity", ["low", "medium", "high"])
def test_outside_quiet_hours_everything_is_delivered(
    notifier, capture, write_config, severity: str
) -> None:
    now = _now_local()
    start, end = window_excluding(now)
    write_config("notifications", _notifications(quiet_start=start, quiet_end=end))

    notifier.dispatch_event("alert", alert_payload(alert_id=f"a_{severity}", severity=severity))

    assert capture.total >= 1, (
        f"a {severity} alert was dropped outside quiet hours {start}-{end}: {capture.describe()}"
    )


def test_quiet_hours_can_be_turned_off_by_config(notifier, capture, write_config) -> None:
    now = _now_local()
    start, end = window_containing(now)
    write_config(
        "notifications", _notifications(quiet_start=start, quiet_end=end, suppress=[])
    )

    notifier.dispatch_event("alert", alert_payload(severity="low"))

    assert capture.total >= 1, "an empty suppress_severities list must suppress nothing"


# -- the window that crosses midnight --------------------------------------


@pytest.mark.parametrize(
    ("moment", "expect_suppressed"),
    [
        (datetime(2026, 7, 20, 22, 30), True),   # after start, before midnight
        (datetime(2026, 7, 20, 23, 59), True),   # last minute of the day
        (datetime(2026, 7, 21, 0, 0), True),     # midnight itself
        (datetime(2026, 7, 21, 3, 0), True),     # after midnight, before end
        (datetime(2026, 7, 21, 5, 59), True),    # last minute of the window
        (datetime(2026, 7, 21, 7, 0), False),    # after the window
        (datetime(2026, 7, 21, 12, 0), False),   # the middle of the day
        (datetime(2026, 7, 21, 20, 59), False),  # a minute before it starts
    ],
)
def test_quiet_hours_window_crosses_midnight(
    notifier,
    capture,
    write_config,
    monkeypatch: pytest.MonkeyPatch,
    moment: datetime,
    expect_suppressed: bool,
) -> None:
    """The shipped window is 21:00-06:00, which wraps. A naive
    ``start <= now <= end`` comparison silently never suppresses anything."""
    write_config("notifications", _notifications(quiet_start="21:00", quiet_end="06:00"))
    freeze_clock(monkeypatch, notifier, moment)

    notifier.dispatch_event("alert", alert_payload(severity="medium"))

    if expect_suppressed:
        assert capture.total == 0, f"{moment:%H:%M} is inside 21:00-06:00: {capture.describe()}"
    else:
        assert capture.total >= 1, f"{moment:%H:%M} is outside 21:00-06:00: {capture.describe()}"


# --------------------------------------------------------------------------
# Rate limit
# --------------------------------------------------------------------------


def test_the_same_alert_for_the_same_entity_is_rate_limited(
    notifier, capture, write_config
) -> None:
    now = _now_local()
    start, end = window_excluding(now)
    write_config("notifications", _notifications(quiet_start=start, quiet_end=end))

    payload = alert_payload(alert_id="client_acceptance_drop", severity="high", entity="agero")

    notifier.dispatch_event("alert", dict(payload))
    assert capture.total == 1, f"the first alert should go out: {capture.describe()}"

    notifier.dispatch_event("alert", dict(payload))
    assert capture.total == 1, (
        f"a repeat inside the 4h window must be suppressed: {capture.describe()}"
    )


def test_the_rate_limit_is_per_entity(notifier, capture, write_config) -> None:
    """Two clients both going bad is two pieces of news, not one."""
    now = _now_local()
    start, end = window_excluding(now)
    write_config("notifications", _notifications(quiet_start=start, quiet_end=end))

    notifier.dispatch_event(
        "alert", alert_payload(alert_id="client_acceptance_drop", severity="high", entity="agero")
    )
    notifier.dispatch_event(
        "alert", alert_payload(alert_id="client_acceptance_drop", severity="high", entity="quest")
    )

    assert capture.total == 2, f"a different entity must not be rate limited: {capture.describe()}"


def test_the_rate_limit_is_per_alert_id(notifier, capture, write_config) -> None:
    now = _now_local()
    start, end = window_excluding(now)
    write_config("notifications", _notifications(quiet_start=start, quiet_end=end))

    notifier.dispatch_event(
        "alert", alert_payload(alert_id="client_acceptance_drop", severity="high", entity="agero")
    )
    notifier.dispatch_event(
        "alert", alert_payload(alert_id="missed_tow", severity="high", entity="agero")
    )

    assert capture.total == 2


def test_the_rate_limit_expires(
    notifier, capture, write_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = datetime(2026, 7, 20, 12, 0)
    write_config("notifications", _notifications(quiet_start="21:00", quiet_end="06:00"))

    freeze_clock(monkeypatch, notifier, base)
    payload = alert_payload(alert_id="client_acceptance_drop", severity="high", entity="agero")
    notifier.dispatch_event("alert", dict(payload))
    assert capture.total == 1

    # Still inside the 4h window.
    freeze_clock(monkeypatch, notifier, base + timedelta(hours=3, minutes=30))
    notifier.dispatch_event("alert", dict(payload))
    assert capture.total == 1, f"3h30m is inside the 4h window: {capture.describe()}"

    # Past it.
    freeze_clock(monkeypatch, notifier, base + timedelta(hours=4, minutes=30))
    notifier.dispatch_event("alert", dict(payload))
    assert capture.total == 2, f"4h30m is past the 4h window: {capture.describe()}"


# --------------------------------------------------------------------------
# pipeline_failure is never suppressed
# --------------------------------------------------------------------------


def test_pipeline_failure_ignores_quiet_hours(notifier, capture, write_config) -> None:
    now = _now_local()
    start, end = window_containing(now)
    write_config(
        "notifications",
        _notifications(
            quiet_start=start, quiet_end=end, suppress=["low", "medium", "high"]
        ),
    )

    notifier.dispatch_event("pipeline_failure", failure_payload())

    assert capture.total >= 1, (
        "pipeline_failure must ignore quiet hours entirely -- a missing report "
        f"has to be louder than a bad one: {capture.describe()}"
    )


def test_pipeline_failure_ignores_the_rate_limit(notifier, capture, write_config) -> None:
    """A pipeline that fails every hour must alert every hour."""
    now = _now_local()
    start, end = window_containing(now)
    write_config(
        "notifications",
        _notifications(
            quiet_start=start,
            quiet_end=end,
            suppress=["low", "medium", "high"],
            rate_limit="24h",
        ),
    )

    notifier.dispatch_event("pipeline_failure", failure_payload())
    notifier.dispatch_event("pipeline_failure", failure_payload())
    notifier.dispatch_event("pipeline_failure", failure_payload())

    assert capture.total >= 3, (
        f"every pipeline failure must be delivered, got {capture.total}: {capture.describe()}"
    )


def test_pipeline_failure_body_says_what_broke(notifier, capture, write_config) -> None:
    now = _now_local()
    start, end = window_excluding(now)
    write_config("notifications", _notifications(quiet_start=start, quiet_end=end))

    notifier.dispatch_event("pipeline_failure", failure_payload())

    bodies = " ".join(capture.bodies()).lower()
    assert "acquisition" in bodies, f"the failing stage is missing from the SMS: {bodies!r}"
    assert "timeout" in bodies, f"the error is missing from the SMS: {bodies!r}"


# --------------------------------------------------------------------------
# What was held back is still recorded
# --------------------------------------------------------------------------


def test_a_suppressed_alert_is_still_recorded(notifier, capture, write_config) -> None:
    """The dashboard shows what was held back, so suppression cannot be a
    silent drop. ``alerts_fired.suppressed_reason`` is the column for it."""
    now = _now_local()
    start, end = window_containing(now)
    write_config("notifications", _notifications(quiet_start=start, quiet_end=end))

    notifier.dispatch_event("alert", alert_payload(severity="low"))

    rows = alerts_fired()
    if not rows:
        pytest.skip("this notifier does not persist alerts_fired rows")
    assert rows[0].suppressed_reason, (
        "a suppressed alert must record why it was held back"
    )


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------


HOURLY_METRICS = {
    "window_start": datetime(2026, 7, 20, 14, 0),
    "hour_start": "14:00",
    "hour_end": "14:59",
    "offered": 12,
    "accepted": 9,
    "rate": 0.75,
    "rate_pct": 75,
    "day_offered": 84,
    "day_accepted": 61,
    "day_running_offered": 84,
    "day_running_accepted": 61,
    "day_rate": 61 / 84,
    "day_rate_pct": 73,
}


def test_the_hourly_sms_has_the_exact_shape_it_replaced(
    notifier, capture, write_config
) -> None:
    """This text replaces a human texting the numbers. It must read the same.

        14:00-14:59 | Offered 12 / Accepted 9 (75%)
        Day: 84 / 61 (73%)
    """
    now = _now_local()
    start, end = window_excluding(now)
    write_config("notifications", _notifications(quiet_start=start, quiet_end=end))

    notifier.dispatch_report("hourly", dict(HOURLY_METRICS))

    assert capture.total >= 1, f"the hourly report was not sent: {capture.describe()}"
    expected = "14:00-14:59 | Offered 12 / Accepted 9 (75%)\nDay: 84 / 61 (73%)"
    bodies = [body.replace("\r\n", "\n").strip() for body in capture.bodies()]
    assert expected in bodies, f"hourly SMS shape drifted.\nexpected: {expected!r}\ngot: {bodies!r}"


def test_a_report_with_no_offers_renders_zero_percent_not_a_crash(
    notifier, capture, write_config
) -> None:
    """formatting.zero_offers_rate_pct is 0: an empty hour still texts."""
    now = _now_local()
    start, end = window_excluding(now)
    write_config("notifications", _notifications(quiet_start=start, quiet_end=end))

    metrics = dict(HOURLY_METRICS)
    metrics.update(
        {
            "hour_start": "03:00",
            "hour_end": "03:59",
            "offered": 0,
            "accepted": 0,
            "rate": None,
            "rate_pct": 0,
        }
    )

    notifier.dispatch_report("hourly", metrics)

    assert capture.total >= 1
    assert any("0%" in body for body in capture.bodies()), capture.describe()


# --------------------------------------------------------------------------
# Missed work -- what every report has to lead with
#
# MISSED_WORK_MODEL.md section 7. The owner's question is "what are we NOT
# accepting", so acceptance rate is supporting context and the inventory of
# missed work is the headline. These tests are about the three things that go
# wrong once that is only a convention: the hourly text growing a line it was
# never meant to have, a ranked list going out with no stated unit, and a
# report implying money the feed cannot produce.
# --------------------------------------------------------------------------


def missed_work_document(
    *,
    unanswered_tows: int = 0,
    missed: int = 40,
    offers: int = 120,
) -> dict[str, Any]:
    """A document shaped exactly like the one agents/missed_work.py emits."""
    return {
        "report_type": "missed_work",
        "totals": {
            "offers": offers,
            "accepted": offers - missed,
            "missed": missed,
            "recoverable": missed - 10,
            "recoverable_excluding_withdrew": missed - 10,
            "recoverable_including_withdrew": missed,
            "withdrew": 10,
            "in_flight": 0,
            "declined": 12,
            "no_response": unanswered_tows + 4,
            "accept_failed": 0,
            "unknown_status": 0,
            "acceptance_rate": (offers - missed) / offers,
            "missed_rate": missed / offers,
            "recoverable_rate": (missed - 10) / offers,
            "no_response_rate": (unanswered_tows + 4) / offers,
            "count_client_withdrew_as_recoverable": False,
        },
        "by_cause": {
            "attention": {
                "cause": "attention",
                "remedy": "alerting",
                "question": "Which hour-of-week window is unmanned?",
                "missed": unanswered_tows + 4,
                "share": 0.5,
                "recoverable": unanswered_tows + 4,
                "buckets": {"no_response": unanswered_tows + 4},
                "service_classes": {"tow": unanswered_tows, "light_service": 4},
                "top_clients": [{"client": "Agero", "missed": unanswered_tows, "share": 1.0}],
            },
        },
        "inventory": [
            {
                "service_class": "tow",
                "cause": "attention",
                "remedy": "alerting",
                "question": "Which hour-of-week window is unmanned?",
                "offers": offers,
                "accepted": offers - missed,
                "missed": unanswered_tows,
                "missed_share": 0.5,
                "class_miss_rate": 0.25,
                "recoverable": unanswered_tows,
                "buckets": {"no_response": unanswered_tows},
                "top_clients": [{"client": "Agero", "missed": unanswered_tows, "share": 1.0}],
                "top_service_types": [
                    {"service_type_raw": "Tow", "missed": unanswered_tows, "share": 1.0}
                ],
            },
            # A light-service row, to prove the SMS line counts only the work
            # the acceptance policy says the owner WANTS.
            {
                "service_class": "light_service",
                "cause": "attention",
                "remedy": "alerting",
                "question": "Which hour-of-week window is unmanned?",
                "offers": 40,
                "accepted": 1,
                "missed": 4,
                "missed_share": 0.1,
                "class_miss_rate": 0.1,
                "recoverable": 4,
                "buckets": {"no_response": 4},
                "top_clients": [],
                "top_service_types": [],
            },
        ],
        "inventory_meta": {
            "restricted_to_should_accept": True,
            "service_classes": ["tow", "winch_out"],
            "rank_by": "job_count",
            "top_n": 10,
            "pairs": 2,
            "missed_in_inventory": unanswered_tows + 4,
            "missed_in_window": missed,
        },
        "blind_spots": {
            "rows": 7,
            "cols": 24,
            "blind_spots": [
                {
                    "weekday": "Sun",
                    "hour": 20,
                    "label": "Sun 20:00",
                    "offers": 19,
                    "accepted": 5,
                    "no_response": 11,
                    "no_response_rate": 0.58,
                    "is_blind_spot": True,
                }
            ],
            "by_hour": [],
            "blind_spot_count": 1,
            "response_window_note": "The median response window Towbook allows is 3 minutes.",
        },
        "closeoff_candidates": {
            "clients": [
                {
                    "client": "Agero",
                    "client_key": "agero",
                    "client_offers": 120,
                    "candidate_offers": 145,
                    "candidate_accepted": 0,
                    "service_types": [
                        {
                            "service_type_raw": "Tire Change",
                            "offers": 145,
                            "accepted": 0,
                            "rate": 0.0,
                            "share_of_client_offers": 0.2,
                        }
                    ],
                }
            ],
            "by_service_type": [
                {"service_type_raw": "Tire Change", "offers": 145, "accepted": 0, "rate": 0.0}
            ],
            "totals": {"offers": 145, "accepted": 0, "service_types": 1, "clients": 1},
            "wanted_baseline": {"service_classes": ["tow", "winch_out"], "offers": 120},
            "rationale": "Refused almost every time they are offered.",
            "ranking_basis": "job_count",
        },
        "client_comparison": {"clients": [], "findings": [], "ranking_basis": "job_count"},
        "ranking_basis": "job_count",
        "revenue_available": False,
        "revenue_note": "offerAmount is empty on 100% of records.",
    }


DAILY_METRICS = {
    "report_type": "daily",
    "date": "2026-07-20",
    "totals": {"offered": 120, "accepted": 80, "denied": 12, "expired": 24, "canceled": 4},
    "rate_pct": 67,
    "by_service_class": [
        {"service_class": "tow", "offered": 100, "accepted": 75, "rate": 0.75},
        {"service_class": "light_service", "offered": 20, "accepted": 5, "rate": 0.25},
    ],
    "by_client": [
        {"client_key": "agero", "client_name": "Agero", "offered": 120, "accepted": 80},
    ],
}


def hourly_with(unanswered_tows: int) -> dict[str, Any]:
    metrics = dict(HOURLY_METRICS)
    metrics["missed_work"] = missed_work_document(unanswered_tows=unanswered_tows)
    return metrics


def test_the_hourly_sms_appends_one_line_when_tows_went_unanswered(
    notifier, capture, write_config
) -> None:
    """The exact two lines, plus one actionable line. Nothing else changes."""
    now = _now_local()
    start, end = window_excluding(now)
    write_config("notifications", _notifications(quiet_start=start, quiet_end=end))

    notifier.dispatch_report("hourly", hourly_with(3))

    bodies = [body.replace("\r\n", "\n").strip() for body in capture.bodies()]
    expected = (
        "14:00-14:59 | Offered 12 / Accepted 9 (75%)\n"
        "Day: 84 / 61 (73%)\n"
        "!! 3 tows unanswered this hour"
    )
    assert expected in bodies, f"hourly missed-work line wrong.\ngot: {bodies!r}"


def test_the_hourly_sms_drops_the_line_entirely_at_zero(
    notifier, capture, write_config
) -> None:
    """Never "!! 0 tows unanswered": that is noise dressed up as a warning.

    The two documented lines must come through byte for byte, with no trailing
    blank line where the third would have been.
    """
    now = _now_local()
    start, end = window_excluding(now)
    write_config("notifications", _notifications(quiet_start=start, quiet_end=end))

    notifier.dispatch_report("hourly", hourly_with(0))

    bodies = [body.replace("\r\n", "\n").strip() for body in capture.bodies()]
    expected = "14:00-14:59 | Offered 12 / Accepted 9 (75%)\nDay: 84 / 61 (73%)"
    assert expected in bodies, f"the quiet-hour shape drifted.\ngot: {bodies!r}"
    assert not any("!!" in body for body in bodies), (
        f"a zero count still produced a warning line: {bodies!r}"
    )


def test_the_hourly_line_counts_only_work_the_policy_says_we_want(notifier) -> None:
    """The document also holds 4 unanswered light-service jobs. They must not count.

    The owner accepts 13 light-service jobs a month against 447 offered; a text
    telling him he "missed" them would be telling him to chase work he
    deliberately declines.
    """
    context = notifier.build_context("hourly", hourly_with(3))
    assert context["unanswered_by_class"] == {"tow": 3}
    assert context["unanswered_wanted"] == 3
    assert context["missed_alert_line"] == "!! 3 tows unanswered this hour"


def test_the_hourly_line_is_singular_for_one_job(notifier) -> None:
    context = notifier.build_context("hourly", hourly_with(1))
    assert context["missed_alert_line"] == "!! 1 tow unanswered this hour"


def test_the_service_class_label_is_config_not_code(notifier, write_config) -> None:
    """Class names are data, so what to call one in a sentence is data too."""
    config = _notifications(quiet_start="00:00", quiet_end="00:00")
    config["formatting"]["service_class_labels"] = {"tow": "hook"}
    write_config("notifications", config)

    context = notifier.build_context("hourly", hourly_with(3))
    assert context["missed_alert_line"] == "!! 3 hooks unanswered this hour"


def test_the_daily_sms_leads_with_missed_work(notifier, capture, write_config) -> None:
    """Missed first, acceptance rate last. The order IS the deliverable."""
    now = _now_local()
    start, end = window_excluding(now)
    config = _notifications(quiet_start=start, quiet_end=end)
    config["routes"].append(
        {"report": "daily", "channel": "sms", "to": ["owner"], "template": "daily_summary"}
    )
    write_config("notifications", config)

    metrics = dict(DAILY_METRICS)
    metrics["missed_work"] = missed_work_document(unanswered_tows=21)
    notifier.dispatch_report("daily", metrics)

    bodies = [body.replace("\r\n", "\n") for body in capture.bodies()]
    assert bodies, capture.describe()
    body = bodies[0]

    first_line = body.splitlines()[0]
    assert "MISSED" in first_line, f"the daily text does not lead with missed work: {body!r}"
    assert body.index("MISSED") < body.index("Accepted"), (
        f"acceptance rate came before the missed count: {body!r}"
    )
    assert "recoverable" in body, body
    # The two actions, each with its number attached.
    assert "Sun 20:00" in body, f"no blind spot in the daily text: {body!r}"
    assert "Tire Change 145/0" in body, f"no close-off candidate: {body!r}"


def test_the_daily_sms_says_the_numbers_are_jobs_not_money(
    notifier, capture, write_config
) -> None:
    """offerAmount is empty on 100% of records. The text has to say so."""
    now = _now_local()
    start, end = window_excluding(now)
    config = _notifications(quiet_start=start, quiet_end=end)
    config["routes"].append(
        {"report": "daily", "channel": "sms", "to": ["owner"], "template": "daily_summary"}
    )
    write_config("notifications", config)

    metrics = dict(DAILY_METRICS)
    metrics["missed_work"] = missed_work_document(unanswered_tows=21)
    notifier.dispatch_report("daily", metrics)

    body = capture.bodies()[0]
    assert "job counts" in body.lower(), f"the daily text does not state its unit: {body!r}"


def test_the_daily_sms_stays_inside_one_message(notifier, write_config) -> None:
    """sms_max_length is 320. A truncated headline is a lost headline."""
    write_config("notifications", _notifications(quiet_start="00:00", quiet_end="00:00"))

    metrics = dict(DAILY_METRICS)
    metrics["missed_work"] = missed_work_document(unanswered_tows=21)
    context = notifier.build_context("daily", metrics)
    message = notifier.render_template("daily_summary", context)

    assert not message.body.endswith("..."), (
        f"the daily SMS was truncated at {len(message.body)} chars:\n{message.body}"
    )


def test_a_report_with_no_missed_work_document_says_so(notifier, write_config) -> None:
    """A failed computation must not read as "MISSED ? of ?".

    That says the business had a bad day when in fact the report did, and the
    two need completely different responses.
    """
    write_config("notifications", _notifications(quiet_start="00:00", quiet_end="00:00"))

    context = notifier.build_context("daily", dict(DAILY_METRICS))
    assert context["missed_available"] is False

    body = notifier.render_template("daily_summary", context).body
    assert "MISSED ?" not in body, body
    assert "unavailable" in body.lower(), body


def test_the_daily_email_leads_with_missed_work_and_states_its_unit(
    notifier, capture, write_config
) -> None:
    now = _now_local()
    start, end = window_excluding(now)
    config = _notifications(quiet_start=start, quiet_end=end)
    config["routes"].append(
        {"report": "daily", "channel": "email", "to": ["owner"], "template": "daily_full"}
    )
    write_config("notifications", config)

    metrics = dict(DAILY_METRICS)
    metrics["missed_work"] = missed_work_document(unanswered_tows=21)
    notifier.dispatch_report("daily", metrics)

    assert capture.email, capture.describe()
    text = notifier._html_to_text(capture.email[0]["body"])

    assert "Ranked by job count" in text, f"no ranking statement in the email:\n{text[:800]}"
    assert "jobs, not dollars" in text.lower(), text[:800]
    assert text.index("Missed") < text.index("Supporting context"), (
        "the daily email did not lead with missed work"
    )
    assert "What we did not get" in text, text[:1200]
    assert "Which hour-of-week window is unmanned?" in text, (
        "the remedy question -- what turns a number into an action -- is missing"
    )


def test_no_report_ever_shows_a_dollar_figure(notifier, write_config) -> None:
    """The one number this feed cannot produce is the one a reader would quote."""
    import re as _re

    write_config("notifications", _notifications(quiet_start="00:00", quiet_end="00:00"))

    metrics = dict(DAILY_METRICS)
    metrics["missed_work"] = missed_work_document(unanswered_tows=21)
    context = notifier.build_context("daily", metrics)

    for template in ("daily_summary", "daily_full"):
        message = notifier.render_template(template, context)
        rendered = f"{message.subject}\n{message.body}"
        # "no $" in the SMS is the disclaimer, not a figure. A figure is a
        # currency sign followed by a digit.
        assert not _re.search(r"[$£€]\s*\d", rendered), (
            f"{template} rendered a currency amount:\n{rendered[:600]}"
        )


def test_the_weekly_email_says_which_causes_are_growing(notifier, write_config) -> None:
    """The weekly's reason to exist: direction, with both numbers behind it."""
    write_config("notifications", _notifications(quiet_start="00:00", quiet_end="00:00"))

    metrics = {
        "report_type": "weekly",
        "week_start": "2026-07-20",
        "totals": {"offered": 120, "accepted": 80, "denied": 12},
        "rate_pct": 67,
        "missed_work": missed_work_document(unanswered_tows=21),
        "missed_work_trend": {
            "totals": {
                "missed": {"missed": 40, "missed_prior": 25, "delta": 15, "direction": "up"},
            },
            "acceptance_rate_pp": -3.0,
            "missed_rate_pp": 4.0,
            "no_response_rate_pp": 5.0,
            "by_cause": {
                "attention": {
                    "cause": "attention",
                    "missed": 25,
                    "missed_prior": 10,
                    "delta": 15,
                    "direction": "up",
                },
                "equipment": {
                    "cause": "equipment",
                    "missed": 8,
                    "missed_prior": 14,
                    "delta": -6,
                    "direction": "down",
                },
            },
            "blind_spots": {
                "current": ["Sun 20:00"],
                "opened": ["Sun 20:00"],
                "closed": ["Mon 18:00"],
                "persisting": [],
            },
            "closeoff_candidates": {
                "current": ["Tire Change"],
                "new": [],
                "resolved": ["Fuel Delivery"],
                "persisting": ["Tire Change"],
            },
            "ranking_basis": "job_count",
        },
    }

    context = notifier.build_context("weekly", metrics)
    text = notifier._html_to_text(notifier.render_template("weekly_full", context).body)

    assert "Which causes are growing" in text, text[:900]
    # Direction in words, and BOTH numbers behind it.
    assert "growing" in text and "shrinking" in text, text[:1500]
    for number in ("25", "10", "8", "14"):
        assert number in text, f"supporting number {number} missing from the weekly trend"
    # Did last week's actions take effect?
    assert "Fuel Delivery" in text, "a close-off that worked was not reported"
    assert "Mon 18:00" in text, "a blind spot that closed was not reported"


def test_the_missed_line_comes_from_the_real_metrics_pipeline(
    notifier, metrics, write_config, capture
) -> None:
    """End to end: seeded rows -> compute_hourly -> the third SMS line.

    The value of this one is that it builds nothing by hand. If compute_hourly
    ever stops carrying the bucket split, the hourly text goes quiet and only
    this test notices.
    """
    from towbook_agent.core.models import Request

    now = _now_local()
    start, end = window_excluding(now)
    write_config("notifications", _notifications(quiet_start=start, quiet_end=end))

    hour = datetime(2026, 7, 20, 14, 0)
    seeded = [
        ("accepted", "Accepted"),
        ("accepted", "Accepted"),
        ("expired", "Expired"),
        ("expired", "Expired"),
        ("expired", "Another Provider Responded"),
    ]
    with get_session() as session:
        for index, (status, label) in enumerate(seeded):
            session.add(
                Request(
                    request_id=f"HR-{index:03d}",
                    account_id="default",
                    client_name="Agero",
                    client_key="agero",
                    offered_at=hour.replace(minute=index * 5),
                    status=status,
                    status_raw=label,
                    service_type_raw="Tow",
                    service_class="tow",
                )
            )

    document = metrics.compute_hourly(hour)
    missed = document.get("missed_work")
    assert isinstance(missed, dict), (
        "compute_hourly must carry the missed-work split, or the hourly SMS can "
        "never append its one actionable line"
    )
    assert missed["totals"]["no_response"] == 3, missed["totals"]

    notifier.dispatch_report("hourly", document)
    bodies = [body.replace("\r\n", "\n").strip() for body in capture.bodies()]
    assert any("!! 3 tows unanswered this hour" in body for body in bodies), (
        f"the pipeline did not produce the missed-work line: {bodies!r}"
    )


def test_the_provenance_disclaimer_never_vanishes_from_the_ranking_caption(
    notifier,
) -> None:
    """Whatever the price book says, the caption must disown the feed.

    ``revenue_available`` says a human typed average job values into
    ``missed_work.job_value_by_client``. Towbook sends no offer amount on any
    record either way, so BOTH captions have to say so -- a disclaimer that
    disappeared on a config edit would be missing on exactly the day a reader
    assumed the ranking was money.

    What the flag may change is only which true sentence is shown. Once values
    are configured the report really does carry ``missed_value``, and the old
    wording -- "these are jobs, not dollars" -- became a false statement printed
    directly underneath a column of dollars. Telling a reader they are not
    looking at money while showing them money is worse than saying nothing, so
    the priced caption is asserted NOT to make that claim.
    """
    priced = missed_work_document(unanswered_tows=3)
    priced["revenue_available"] = True

    unpriced_context = notifier.build_context(
        "daily", {**DAILY_METRICS, "missed_work": missed_work_document(unanswered_tows=3)}
    )
    priced_context = notifier.build_context("daily", {**DAILY_METRICS, "missed_work": priced})

    assert priced_context["revenue_available"] is True
    assert unpriced_context["revenue_available"] is False

    priced_note = priced_context["ranking_note"]
    unpriced_note = unpriced_context["ranking_note"]

    # The invariant that actually protects the reader: neither caption may let
    # anyone believe Towbook supplied an amount.
    for note in (priced_note, unpriced_note):
        assert "job count" in note, note
        lowered = note.lower()
        assert "no offer amounts" in lowered or "does not send offer amounts" in lowered, note

    # Unpriced: it is true that there are no dollars, and it is said.
    assert "not dollars" in unpriced_note

    # Priced: dollars ARE on the page, so the caption must not deny them, and
    # must name where they came from and what they exclude.
    assert "not dollars" not in priced_note
    assert "estimates" in priced_note.lower()
    assert "rules.yaml" in priced_note
    assert "margin" in priced_note.lower()
    assert priced_note != unpriced_note


# --------------------------------------------------------------------------
# The reporting timezone
#
# The whole suite runs at TZ=UTC (conftest says so, and says why: local and
# stored UTC coincide, so a hand-checked number stays hand-checkable). That
# also means every local/UTC confusion in the notifier is a no-op here and
# invisible. These two tests set a real offset zone so it is not.
# --------------------------------------------------------------------------


def test_the_hourly_label_is_the_local_hour_not_a_second_conversion(
    notifier, monkeypatch
) -> None:
    """The hour on the SMS must be the hour the counts are for.

    ``compute_hourly`` emits ``window_start`` from ``start_local`` -- already in
    the reporting timezone, with ``timezone`` stated beside it -- and
    ``window_start_utc`` for the same moment in UTC. Both are naive ISO strings,
    so only the KEY says which clock each is on.

    Reading the local one with a helper that assumes naive-means-UTC subtracts
    the offset a second time: the counts are for 18:00 and the label reads
    14:00. Live against the real account this was a four-hour error on the
    message this system sends most often.
    """
    monkeypatch.setenv("TZ", "America/Detroit")

    context = notifier.build_context(
        "hourly",
        {
            "report_type": "hourly",
            "timezone": "America/Detroit",
            "window_start": "2026-07-27T18:00:00",
            "window_end": "2026-07-27T19:00:00",
            "window_start_utc": "2026-07-27T22:00:00",
            "window_end_utc": "2026-07-27T23:00:00",
            "date": "2026-07-27",
            "offered": 9,
            "accepted": 0,
        },
    )

    assert context["hour_start"] == "18:00", (
        f"the hourly SMS labelled an 18:00 local window {context['hour_start']!r}. "
        "window_start is local; converting it as if it were UTC shifts every "
        "hourly text by the UTC offset."
    )
    assert context["hour_end"] == "18:59"
    assert context["date"] == "2026-07-27"


def test_an_early_hour_does_not_roll_the_date_back_a_day(notifier, monkeypatch) -> None:
    """00:00-03:59 local is the case a UTC/local mix-up moves to yesterday.

    At TZ=UTC this cannot fail, which is exactly why it is pinned at an offset.
    """
    monkeypatch.setenv("TZ", "America/Detroit")

    context = notifier.build_context(
        "hourly",
        {
            "report_type": "hourly",
            "timezone": "America/Detroit",
            "window_start": "2026-07-27T02:00:00",
            "window_end": "2026-07-27T03:00:00",
            "window_start_utc": "2026-07-27T06:00:00",
            "window_end_utc": "2026-07-27T07:00:00",
            "date": "2026-07-27",
            "offered": 1,
            "accepted": 0,
        },
    )

    assert context["hour_start"] == "02:00"
    assert context["date"] == "2026-07-27", (
        "a 02:00 local hour reported under the previous calendar day"
    )
