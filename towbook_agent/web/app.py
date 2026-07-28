"""The dashboard: FastAPI + Jinja2, server-rendered, no build step.

Nine views, in the order an owner actually uses them:

    /             Missed work -- what we did NOT get, why, and what to do
    /blind-spots  Blind spots -- which hours go unanswered (the staffing case)
    /close-off    Close-off   -- work we do not want, grouped by the client
                                 to have the conversation with
    /live         Live        -- what is happening right now
    /daily        Daily       -- what happened yesterday, and why
    /clients      Clients     -- who is sending work and how we treat them
    /trends       Trends      -- when we are good and when we are not
    /rules        Rules       -- what the machine believes, and what it wants to
    /health       Health      -- whether any of the above can be trusted

WHY MISSED WORK IS THE FRONT PAGE
---------------------------------
The system was originally framed around acceptance rate. The owner's question
is narrower: *what are we not accepting, do we want it, and what would it take
to accept it -- or how do we stop it being offered at all?* Acceptance rate is
a symptom of that, so it is supporting context on every screen and the headline
of none. ``/`` is the inventory of missed work; the old Live view still exists,
unchanged, at ``/live``. See MISSED_WORK_MODEL.md.

**No screen in this app shows a dollar figure.** ``offerAmount`` is empty on
100% of the records this account produces, so every ranked list says
"ranked by job count - offer amounts are not populated by Towbook" and means it.

The whole app is **read-only against the datastore with exactly one
exception**: accepting or rejecting a proposed rule change on /rules. That is
the human action the Analyst is forbidden to take (hard constraint #3), so it
is the one thing on the dashboard that writes -- and it writes to a config
file, never to the data. See :mod:`towbook_agent.web.rules_admin`.

Charts read JSON from the small ``/api/...`` endpoints and are drawn by the
vendored Chart.js in ``static/``. The hour-of-week heatmap and the client
sparklines are rendered server-side as HTML/SVG instead: they need no
interaction beyond a tooltip, and rendering them in the template means they
survive with JavaScript disabled.

Run it with ``python -m towbook_agent serve``, or directly:

    uvicorn towbook_agent.web.app:app --port 8080
"""

from __future__ import annotations

import logging
import os
from datetime import date as _date
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Query, Request as HTTPRequest
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..core.logging_setup import redact, setup_logging
from ..core.paths import STATIC_DIR, TEMPLATES_DIR, ensure_dirs
from . import queries as q
from . import rules_admin
from .rules_admin import RulesWriteError

__all__ = ["app", "create_app", "serve"]

logger = logging.getLogger(__name__)

NAV = (
    ("missed", "Missed work", "/"),
    ("blind_spots", "Blind spots", "/blind-spots"),
    ("closeoff", "Close-off", "/close-off"),
    ("live", "Live", "/live"),
    ("daily", "Daily", "/daily"),
    ("clients", "Clients", "/clients"),
    ("trends", "Trends", "/trends"),
    ("rules", "Rules", "/rules"),
    ("health", "Health", "/health"),
)

SEED_COMMAND = "python -m towbook_agent seed"

#: How stale the most recent run may be before the header says so.
STALE_RUN_HOURS = 3.0


# ==========================================================================
# Template filters
# ==========================================================================
#
# Every one of these renders None as an em-dash. That is the single most
# important formatting rule in the app: a rate of None means "we were offered
# nothing", and printing that as 0% would tell the owner he turned down work
# that was never sent to him.

DASH = "—"


def f_pct(value: Any, decimals: int = 0) -> str:
    """A rate (0..1) as a percentage. None -> em-dash."""
    if value is None or isinstance(value, bool):
        return DASH
    try:
        return f"{float(value) * 100:.{decimals}f}%"
    except (TypeError, ValueError):
        return DASH


def f_points(value: Any, decimals: int = 0) -> str:
    """A rate *difference* in percentage points, signed. None -> em-dash."""
    if value is None:
        return DASH
    try:
        return f"{float(value) * 100:+.{decimals}f} pts"
    except (TypeError, ValueError):
        return DASH


def f_num(value: Any) -> str:
    if value is None:
        return DASH
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def f_money(value: Any) -> str:
    if value in (None, ""):
        return DASH
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return DASH


def f_dt(value: Any, fmt: str = "%b %d, %Y %I:%M %p") -> str:
    if not isinstance(value, (datetime, _date)):
        return DASH
    return value.strftime(fmt).replace(" 0", " ").lstrip("0")


def f_time(value: Any) -> str:
    if not isinstance(value, datetime):
        return DASH
    return value.strftime("%I:%M %p").lstrip("0")


def f_day(value: Any, fmt: str = "%a %b %d") -> str:
    if not isinstance(value, (datetime, _date)):
        return DASH
    return value.strftime(fmt)


def f_ago(value: Any) -> str:
    """Human-readable age of an aware local datetime."""
    if not isinstance(value, datetime):
        return DASH
    try:
        seconds = (q.now_local() - value).total_seconds()
    except TypeError:
        return DASH
    if seconds < 0:
        return "just now"
    if seconds < 90:
        return f"{int(seconds)}s ago"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 36:
        return f"{hours:.1f}h ago"
    return f"{hours / 24:.1f}d ago"


def f_duration(value: Any) -> str:
    if value is None:
        return DASH
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DASH
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"


def f_nice(value: Any) -> str:
    """snake_case identifier -> readable label."""
    if value in (None, ""):
        return DASH
    return str(value).replace("_", " ").strip().capitalize()


def f_truncate_mid(value: Any, length: int = 60) -> str:
    text = "" if value is None else str(value)
    if len(text) <= length:
        return text or DASH
    keep = (length - 1) // 2
    return f"{text[:keep]}…{text[-keep:]}"


# ==========================================================================
# Application
# ==========================================================================


def create_app() -> FastAPI:
    ensure_dirs()
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging()

    application = FastAPI(
        title="Towbook Job Acceptance Intelligence",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters.update(
        {
            "pct": f_pct,
            "points": f_points,
            "num": f_num,
            "money": f_money,
            "dt": f_dt,
            "clock": f_time,
            "day": f_day,
            "ago": f_ago,
            "duration": f_duration,
            "nice": f_nice,
            "midtrunc": f_truncate_mid,
        }
    )
    templates.env.globals.update(
        {
            "NAV": NAV,
            "SEED_COMMAND": SEED_COMMAND,
            "DASH": DASH,
            "APP_VERSION": __version__,
            "band_for": q.rate_band,
            "miss_band_for": q.miss_band,
            "EMPTY_CLIENT_KEY": q.EMPTY_CLIENT_KEY,
            "STATUS_ORDER": q.STATUS_ORDER,
            "WEEKDAY_LABELS": q.WEEKDAY_LABELS,
            "RANKING_NOTE": q.RANKING_NOTE,
            "RESPONSE_WINDOW": q.RESPONSE_WINDOW,
            "next_dir": _next_dir,
            "qs": _query_string,
        }
    )
    application.state.templates = templates
    return application


def _next_dir(current_sort: str, current_dir: str, column: str) -> str:
    """Direction a column header should request when clicked."""
    if current_sort != column:
        return "desc"
    return "asc" if current_dir == "desc" else "desc"


def _query_string(**params: Any) -> str:
    clean = {key: value for key, value in params.items() if value not in (None, "")}
    return ("?" + urlencode(clean)) if clean else ""


app = create_app()


def _render(
    request: HTTPRequest, name: str, context: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request, name=name, context=context, status_code=status_code
    )


def _shell(request: HTTPRequest, active: str, **extra: Any) -> dict[str, Any]:
    """Context every full page needs: nav state, clock, staleness, empty state."""
    last_run = extra.pop("last_run", None)
    if last_run is None:
        last_run = q.last_run_summary()
    stale = bool(
        last_run
        and last_run.get("age_hours") is not None
        and last_run["age_hours"] > STALE_RUN_HOURS
    )
    context: dict[str, Any] = {
        "active": active,
        "now": q.now_local(),
        "timezone": str(q.local_tz()),
        "thresholds": q.rate_thresholds(),
        "has_data": q.has_any_data(),
        "last_run": last_run,
        "run_is_stale": stale,
        "accounts": q.available_accounts(),
        "account": extra.pop("account", None),
        "notice": request.query_params.get("notice") or None,
        "notice_level": request.query_params.get("level") or "info",
    }
    context.update(extra)
    return context


def _account(request: HTTPRequest) -> str | None:
    value = (request.query_params.get("account") or "").strip()
    return value or None


def _days(request: HTTPRequest, default: int = q.MISSED_WORK_WINDOW_DAYS) -> int:
    """A ``?days=`` window, clamped. Anything unparseable falls back."""
    try:
        return max(1, min(int(request.query_params.get("days") or default), 365))
    except (TypeError, ValueError):
        return default


#: Windows offered on the missed-work views. 30 days is the default because it
#: is the shortest window in which a 7 x 24 grid has enough offers per cell to
#: read as anything.
DAY_OPTIONS = (7, 14, 30, 60, 90)


# ==========================================================================
# View 0 -- Missed work (the primary view)
# ==========================================================================


@app.get("/", response_class=HTMLResponse)
def view_missed_work(request: HTTPRequest) -> HTMLResponse:
    """The inventory of work we did not get. The front page.

    Leads with the recoverable count and the four buckets, then the
    ``(service_class, cause)`` inventory with a remedy attached to each row.
    Acceptance rate appears once, as context, at the bottom of the tiles.
    """
    account = _account(request)
    days = _days(request)
    data = q.missed_work_snapshot(days=days, account_id=account)
    return _render(
        request,
        "missed.html",
        _shell(request, "missed", missed=data, account=account, day_options=DAY_OPTIONS),
    )


@app.get("/blind-spots", response_class=HTMLResponse)
def view_blind_spots(request: HTTPRequest) -> HTMLResponse:
    """When offers go unanswered, as a 7 x 24 hour-of-week grid.

    The staffing-decision view: it turns "we are missing work" into "nobody is
    covering these specific hours", which is an argument with evidence attached.
    """
    account = _account(request)
    days = _days(request)
    data = q.blind_spots_snapshot(days=days, account_id=account)
    return _render(
        request,
        "blind_spots.html",
        _shell(request, "blind_spots", spots=data, account=account, day_options=DAY_OPTIONS),
    )


@app.get("/close-off", response_class=HTMLResponse)
def view_closeoff(request: HTTPRequest) -> HTMLResponse:
    """Work we do not want, grouped by the client who keeps sending it.

    Grouped by client because the action is a conversation with that client, so
    each group carries a paste-ready summary of exactly that conversation.
    """
    account = _account(request)
    days = _days(request)
    data = q.closeoff_snapshot(days=days, account_id=account)
    return _render(
        request,
        "closeoff.html",
        _shell(request, "closeoff", closeoff=data, account=account, day_options=DAY_OPTIONS),
    )


# ==========================================================================
# View 1 -- Live
# ==========================================================================


@app.get("/live", response_class=HTMLResponse)
def view_live(request: HTTPRequest) -> HTMLResponse:
    account = _account(request)
    data = q.live_snapshot(account_id=account)
    return _render(
        request,
        "live.html",
        _shell(request, "live", live=data, account=account, last_run=data.get("last_run")),
    )


@app.get("/partials/live", response_class=HTMLResponse)
def partial_live(request: HTTPRequest) -> HTMLResponse:
    """Body of the Live view, for the HTMX polling refresh."""
    account = _account(request)
    data = q.live_snapshot(account_id=account)
    return _render(
        request,
        "partials/live_body.html",
        {
            "live": data,
            "account": account,
            "thresholds": q.rate_thresholds(),
            "now": q.now_local(),
        },
    )


# ==========================================================================
# View 2 -- Daily
# ==========================================================================


def _daily_context(request: HTTPRequest) -> dict[str, Any]:
    params = request.query_params
    account = _account(request)
    # Default to yesterday: the daily report is a post-mortem of a finished day.
    day = q.parse_date(params.get("date"), q.today_local() - timedelta(days=1))
    sort = params.get("sort") or "volume"
    direction = params.get("dir") or "desc"
    data = q.daily_snapshot(day, account_id=account, sort=sort, direction=direction)
    return {
        "daily": data,
        "account": account,
        "thresholds": q.rate_thresholds(),
        "has_data": q.has_any_data(),
        "SEED_COMMAND": SEED_COMMAND,
    }


@app.get("/daily", response_class=HTMLResponse)
def view_daily(request: HTTPRequest) -> HTMLResponse:
    context = _daily_context(request)
    return _render(request, "daily.html", _shell(request, "daily", **context))


@app.get("/partials/daily", response_class=HTMLResponse)
def partial_daily(request: HTTPRequest) -> HTMLResponse:
    """Body of the Daily view -- swapped in when the date picker changes."""
    return _render(request, "partials/daily_body.html", _daily_context(request))


# ==========================================================================
# View 3 -- Clients
# ==========================================================================


def _clients_context(request: HTTPRequest) -> dict[str, Any]:
    params = request.query_params
    account = _account(request)
    sort = params.get("sort") or "volume"
    direction = params.get("dir") or "desc"
    data = q.clients_overview(account_id=account, sort=sort, direction=direction)
    return {
        "overview": data,
        "account": account,
        "thresholds": q.rate_thresholds(),
        "has_data": q.has_any_data(),
    }


@app.get("/clients", response_class=HTMLResponse)
def view_clients(request: HTTPRequest) -> HTMLResponse:
    context = _clients_context(request)
    return _render(request, "clients.html", _shell(request, "clients", **context))


@app.get("/partials/clients", response_class=HTMLResponse)
def partial_clients(request: HTTPRequest) -> HTMLResponse:
    """The sortable client table, swapped in on a column-header click."""
    return _render(request, "partials/client_table.html", _clients_context(request))


@app.get("/clients/{slug:path}", response_class=HTMLResponse)
def view_client_detail(request: HTTPRequest, slug: str) -> HTMLResponse:
    account = _account(request)
    slug = (slug or "").strip("/") or q.EMPTY_CLIENT_KEY
    try:
        days = max(1, min(int(request.query_params.get("days") or 30), 365))
    except ValueError:
        days = 30
    data = q.client_detail(slug, days=days, account_id=account)
    return _render(
        request,
        "client_detail.html",
        _shell(request, "clients", client=data, account=account),
    )


# ==========================================================================
# View 4 -- Trends
# ==========================================================================


@app.get("/trends", response_class=HTMLResponse)
def view_trends(request: HTTPRequest) -> HTMLResponse:
    account = _account(request)
    try:
        weeks = int(request.query_params.get("weeks") or 8)
    except ValueError:
        weeks = 8
    data = q.trends_snapshot(weeks=weeks, account_id=account)
    return _render(
        request,
        "trends.html",
        _shell(request, "trends", trends=data, account=account, week_options=(4, 8, 13, 26)),
    )


# ==========================================================================
# View 5 -- Rules
# ==========================================================================


@app.get("/rules", response_class=HTMLResponse)
def view_rules(request: HTTPRequest) -> HTMLResponse:
    data = q.rules_view()
    data["backups"] = rules_admin.list_backups(limit=10)
    data["allowed_targets"] = sorted(rules_admin.ALLOWED_PATCH_TARGETS)
    return _render(request, "rules.html", _shell(request, "rules", rules=data))


def _rules_redirect(message: str, level: str) -> RedirectResponse:
    query = urlencode({"notice": redact(message)[:900], "level": level})
    return RedirectResponse(url=f"/rules?{query}", status_code=303)


@app.post("/rules/proposals/{proposal_id}/accept")
def accept_rule_proposal(
    proposal_id: str,
    reviewer: str = Form(default="dashboard"),
) -> RedirectResponse:
    """Splice a proposal's patch into rules.yaml. The human action.

    rules.yaml is backed up before the write, the patched text is validated
    before it replaces the file, and a conflicting patch is refused outright.
    """
    try:
        result = rules_admin.accept_proposal(proposal_id, reviewer=(reviewer or "dashboard").strip())
    except RulesWriteError as exc:
        logger.warning("proposal %s not accepted: %s", proposal_id, exc)
        return _rules_redirect(str(exc), "error")
    except Exception as exc:  # pragma: no cover - filesystem dependent
        logger.exception("accepting proposal %s failed", proposal_id)
        return _rules_redirect(f"{type(exc).__name__}: {exc}", "error")

    message = result["message"]
    if result.get("warning"):
        return _rules_redirect(f"{message} {result['warning']}", "warn")
    return _rules_redirect(message, "ok")


@app.post("/rules/proposals/{proposal_id}/reject")
def reject_rule_proposal(
    proposal_id: str,
    reviewer: str = Form(default="dashboard"),
) -> RedirectResponse:
    try:
        result = rules_admin.reject_proposal(proposal_id, reviewer=(reviewer or "dashboard").strip())
    except RulesWriteError as exc:
        logger.warning("proposal %s not rejected: %s", proposal_id, exc)
        return _rules_redirect(str(exc), "error")
    except Exception as exc:  # pragma: no cover - filesystem dependent
        logger.exception("rejecting proposal %s failed", proposal_id)
        return _rules_redirect(f"{type(exc).__name__}: {exc}", "error")
    return _rules_redirect(result["message"], "ok")


# ==========================================================================
# View 6 -- Health
# ==========================================================================


@app.get("/health", response_class=HTMLResponse)
def view_health(request: HTTPRequest) -> HTMLResponse:
    data = q.health_view()
    return _render(request, "health.html", _shell(request, "health", health=data))


@app.get("/healthz")
def liveness() -> JSONResponse:
    """Machine-readable liveness probe. No secrets: the DB URL is masked."""
    database = q.core_db.healthcheck()
    payload = {
        "ok": bool(database.get("ok")),
        "version": __version__,
        "database": {"ok": database.get("ok"), "tables": len(database.get("tables") or [])},
        "has_data": q.has_any_data(),
        "timezone": str(q.local_tz()),
    }
    return JSONResponse(payload, status_code=200 if payload["ok"] else 503)


# ==========================================================================
# JSON for the charts
# ==========================================================================


@app.get("/api/missed-work")
def api_missed_work(
    days: int = Query(default=q.MISSED_WORK_WINDOW_DAYS, ge=1, le=365),
    account: str | None = Query(default=None),
) -> JSONResponse:
    data = q.missed_work_snapshot(days=days, account_id=account or None)
    return JSONResponse(
        q.jsonable(
            {
                "available": data["available"],
                "error": data["error"],
                "days": data["days"],
                "first_day": data["first_day"],
                "last_day": data["last_day"],
                "totals": data["totals"],
                "headline_buckets": data["headline_buckets"],
                "secondary_buckets": data["secondary_buckets"],
                "inventory": data["inventory"],
                "inventory_meta": data["inventory_meta"],
                "causes": data["causes"],
                "blind_spots": data["blind_spots"],
                "closeoff": data["closeoff"],
                "findings": data["findings"],
                "bucket_sources": data["bucket_sources"],
                "unknown_statuses": data["unknown_statuses"],
                # Stated in the payload, not only on the page, so a consumer
                # of this endpoint cannot present these counts as revenue.
                "ranking_basis": data["ranking_basis"],
                "ranking_note": data["ranking_note"],
                "has_data": data["has_data"],
            }
        )
    )


@app.get("/api/blind-spots")
def api_blind_spots(
    days: int = Query(default=q.MISSED_WORK_WINDOW_DAYS, ge=1, le=365),
    account: str | None = Query(default=None),
) -> JSONResponse:
    data = q.blind_spots_snapshot(days=days, account_id=account or None)
    return JSONResponse(
        q.jsonable(
            {
                "available": data["available"],
                "error": data["error"],
                "days": data["days"],
                "grid": data["grid"],
                "by_hour": data["by_hour"],
                "worst_cells": data["worst_cells"],
                "worst_hours": data["worst_hours"],
                "blind_spot_count": data["blind_spot_count"],
                "totals": data["totals"],
                "thresholds": data["thresholds"],
                "response_window": data["response_window"],
                "has_data": data["has_data"],
            }
        )
    )


@app.get("/api/close-off")
def api_close_off(
    days: int = Query(default=q.MISSED_WORK_WINDOW_DAYS, ge=1, le=365),
    account: str | None = Query(default=None),
) -> JSONResponse:
    data = q.closeoff_snapshot(days=days, account_id=account or None)
    return JSONResponse(
        q.jsonable(
            {
                "available": data["available"],
                "error": data["error"],
                "days": data["days"],
                "clients": data["clients"],
                "by_service_type": data["by_service_type"],
                "totals": data["totals"],
                "thresholds": data["thresholds"],
                "wanted_baseline": data["wanted_baseline"],
                "ranking_note": data["ranking_note"],
                "has_data": data["has_data"],
            }
        )
    )


@app.get("/api/live")
def api_live(account: str | None = Query(default=None)) -> JSONResponse:
    data = q.live_snapshot(account_id=account or None)
    return JSONResponse(
        q.jsonable(
            {
                "date": data["date"],
                "generated_at": data["generated_at"],
                "totals": data["totals"],
                "hours": data["hours"],
                "baseline_7d": data["baseline_7d"],
                "baseline_30d": data["baseline_30d"],
                "status_mix": data["status_mix"],
                "has_data": data["has_data"],
            }
        )
    )


@app.get("/api/daily")
def api_daily(
    date: str | None = Query(default=None),
    account: str | None = Query(default=None),
) -> JSONResponse:
    day = q.parse_date(date, q.today_local() - timedelta(days=1))
    data = q.daily_snapshot(day, account_id=account or None)
    return JSONResponse(
        q.jsonable(
            {
                "date": data["date"],
                "totals": data["totals"],
                "hours": data["hours"],
                "service_classes": data["service_classes"],
                "denial_mix": data["denial_mix"],
                "clients": [
                    {
                        "client_name": entry["client_name"],
                        "offered": entry["offered"],
                        "accepted": entry["accepted"],
                        "rate": entry["rate"],
                        "delta": entry["delta"],
                    }
                    for entry in data["clients"]
                ],
                "has_data": data["has_data"],
            }
        )
    )


@app.get("/api/clients")
def api_clients(account: str | None = Query(default=None)) -> JSONResponse:
    data = q.clients_overview(account_id=account or None)
    return JSONResponse(
        q.jsonable(
            {
                "window_days": data["window_days"],
                "totals": data["totals"],
                "clients": [
                    {
                        key: entry.get(key)
                        for key in (
                            "client_key",
                            "client_name",
                            "offered",
                            "accepted",
                            "rate",
                            "rate_24h",
                            "rate_7d",
                            "rate_30d",
                            "sparkline",
                            # First-class, not an afterthought: the gap between
                            # clients in how often their offers go unanswered is
                            # the single most actionable per-client number.
                            "no_response",
                            "no_response_rate",
                            "no_response_outlier",
                        )
                    }
                    for entry in data["clients"]
                ],
                "no_response_findings": data["no_response_findings"],
                "has_data": data["has_data"],
            }
        )
    )


@app.get("/api/clients/{slug:path}")
def api_client_detail(slug: str, days: int = Query(default=30, ge=1, le=365)) -> JSONResponse:
    data = q.client_detail((slug or "").strip("/") or q.EMPTY_CLIENT_KEY, days=days)
    return JSONResponse(
        q.jsonable(
            {
                "client_key": data["client_key"],
                "client_name": data["client_name"],
                "totals": data["totals"],
                "trajectory": data["trajectory"],
                "services": data["services"],
                "denial_mix": data["denial_mix"],
                "has_data": data["has_data"],
            }
        )
    )


@app.get("/api/trends")
def api_trends(
    weeks: int = Query(default=8, ge=1, le=26),
    account: str | None = Query(default=None),
) -> JSONResponse:
    data = q.trends_snapshot(weeks=weeks, account_id=account or None)
    return JSONResponse(
        q.jsonable(
            {
                "weeks": data["weeks"],
                "first_day": data["first_day"],
                "last_day": data["last_day"],
                "volume": data["volume"],
                "overall_weekly": data["overall_weekly"],
                "week_labels": data["week_labels"],
                "trajectories": data["trajectories"],
                "heatmap": data["heatmap"],
                "has_data": data["has_data"],
            }
        )
    )


@app.get("/api/health")
def api_health() -> JSONResponse:
    data = q.health_view()
    return JSONResponse(
        q.jsonable(
            {
                "database": {
                    "ok": data["database"].get("ok"),
                    "tables": data["database"].get("tables"),
                    "error": data["database"].get("error"),
                },
                "counts": data["counts"],
                "coverage": data["coverage"],
                "last_success": {
                    key: {
                        "status": (value.get("run") or {}).get("status"),
                        "started_at": (value.get("run") or {}).get("started_at"),
                        "row_count": (value.get("run") or {}).get("row_count"),
                        "age_hours": value.get("age_hours"),
                    }
                    for key, value in data["last_success"].items()
                },
                "failures": len(data["failures"]),
                "rules_version": data["rules_version"],
                "has_data": data["has_data"],
            }
        )
    )


# ==========================================================================
# Errors
# ==========================================================================


@app.exception_handler(404)
def not_found(request: HTTPRequest, exc: Exception) -> HTMLResponse:
    return _render(
        request,
        "error.html",
        _shell(
            request,
            "",
            status_code=404,
            title="Not found",
            detail=f"Nothing is served at {request.url.path}",
        ),
        status_code=404,
    )


@app.exception_handler(Exception)
def unhandled(request: HTTPRequest, exc: Exception) -> HTMLResponse:
    """Render a readable failure instead of a bare 500.

    The message is passed through the secret redactor before it reaches the
    page: a traceback can easily carry a database URL or a token, and the
    dashboard must never be the thing that leaks one.
    """
    logger.exception("unhandled error rendering %s", request.url.path)
    return _render(
        request,
        "error.html",
        _shell(
            request,
            "",
            status_code=500,
            title="Something went wrong rendering this page",
            detail=redact(f"{type(exc).__name__}: {exc}"),
        ),
        status_code=500,
    )


# ==========================================================================
# Entry point used by `python -m towbook_agent serve`
# ==========================================================================


def serve(host: str | None = None, port: int | None = None, reload: bool = False) -> None:
    """Run the dashboard with uvicorn."""
    import uvicorn

    host = host or os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    if port is None:
        try:
            port = int(os.environ.get("DASHBOARD_PORT", "8080"))
        except ValueError:
            port = 8080
    logger.info("dashboard starting on http://%s:%s", host, port)
    if reload:
        uvicorn.run("towbook_agent.web.app:app", host=host, port=port, reload=True)
    else:
        uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    serve()
