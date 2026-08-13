"""The dashboard: FastAPI + Jinja2, server-rendered, no build step.

THE BOARD IS THE DELIVERY MECHANISM
-----------------------------------
Nothing is texted and nothing is emailed any more. The owner opens this in a
browser several times a day, so anything that used to arrive as a message has
to be on a screen here or it is gone. Top-level navigation is therefore four
tabs, in the order they were asked for:

    /hourly       HOURLY   -- today hour by hour. This replaces the hourly SMS
                              outright, carries every line that text carried,
                              and refreshes itself every 60 seconds.
    /weekly       WEEKLY   -- this week against last: coverage split, causes,
                              and what to do about them.
    /monthly      MONTHLY  -- this month against last: trend per cause, client
                              trajectories, and whether the close-offs worked.
    /trends       TRENDS   -- the patterns a single period cannot show: the
                              7 x 24 blind-spot grid, coverage over time,
                              client trajectories, volume, close-off candidates.

The detail views the four tabs summarise are all still here, unchanged, one
click away in the second navigation row -- and every URL that existed before
this restructuring still resolves to the same page:

    /             Missed work -- what we did NOT get, why, and what to do
    /blind-spots  Blind spots -- which hours go unanswered (the staffing case)
    /close-off    Close-off   -- work we do not want, grouped by the client
                                 to have the conversation with
    /live         Live        -- what is happening right now
    /daily        Daily       -- what happened yesterday, and why
    /clients      Clients     -- who is sending work and how we treat them
    /rules        Rules       -- what the machine believes, and what it wants to
    /health       Health      -- whether any of the above can be trusted

THE WHOLE BOARD IS BEHIND A PASSWORD
------------------------------------
A single shared password, default ``1234``, with a signed session cookie. It
was asked for in exactly those terms and is implemented in exactly those terms.
``1234`` is **not** adequate for real customer data, still less for several
towing companies' data behind one login; that is stated here, in the README,
and on the login page itself while the default is still in place. See
:mod:`towbook_agent.web.auth`. ``/healthz`` is exempt so Railway's health check
passes without credentials.

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
from contextlib import asynccontextmanager
from datetime import date as _date
from datetime import datetime, timedelta
from typing import Any, AsyncIterator
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, HTTPException, Query, Request as HTTPRequest
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..core import companies as _companies
from ..core.logging_setup import redact, setup_logging
from ..core.paths import STATIC_DIR, TEMPLATES_DIR, ensure_dirs
from . import accounts
from . import auth
from . import queries as q
from . import rules_admin
from .auth import PasswordGateMiddleware
from .rules_admin import RulesWriteError

__all__ = ["app", "create_app", "serve"]

logger = logging.getLogger(__name__)

#: The four tabs, and the top-level navigation. Order is the order asked for.
TABS = (
    ("hourly", "Hourly", "/hourly"),
    ("weekly", "Weekly", "/weekly"),
    ("monthly", "Monthly", "/monthly"),
    ("trends", "Trends", "/trends"),
)

#: The detail views the tabs summarise. Second row, one click away, every one
#: of them on the URL it has always had -- no redirects, nothing renamed.
DETAIL_NAV = (
    ("missed", "Missed work", "/"),
    ("revenue", "Lost revenue", "/revenue"),
    ("maps", "Maps", "/maps"),
    ("blind_spots", "Blind spots", "/blind-spots"),
    ("closeoff", "Close-off", "/close-off"),
    ("live", "Live", "/live"),
    ("daily", "Daily", "/daily"),
    ("clients", "Clients", "/clients"),
    ("rules", "Rules", "/rules"),
    ("health", "Health", "/health"),
)

#: Kept as the union so anything reading ``NAV`` still sees every destination.
NAV = TABS + DETAIL_NAV

SEED_COMMAND = "python -m towbook_agent seed"

#: How often the Hourly tab pulls a fresh body. It is standing in for a text
#: message that arrived once an hour, so a minute is generous; it is a partial
#: HTML swap of one page, not a poll of the pipeline.
HOURLY_REFRESH_SECONDS = 60

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


def f_money0(value: Any) -> str:
    """Whole dollars, no cents.

    Every dollar figure in this system is an estimate built from per-client
    AVERAGES, so cents are not merely noise -- printing ``$56,595.00`` implies
    a precision the input never had. The headline figures use this; the price
    book itself still prints exact amounts, because those are typed by a human.
    """
    if value in (None, ""):
        return DASH
    try:
        return f"${float(value):,.0f}"
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


#: What the startup hook did, for /health and /healthz. Populated by
#: :func:`lifespan`; empty until the app has actually been served, which is the
#: honest answer for a TestClient that never entered its context manager.
BOOT: dict[str, Any] = {
    "migration": None,
    "scheduler": None,
    "storage_warning": None,
    "accounts": None,
}


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Boot the deployment: migrate, then start scheduling. Then serve.

    ORDER MATTERS AND SO DOES NOT CRASHING.

    1. ``alembic upgrade head``. A container gets a fresh, empty Postgres the
       first time it is deployed and an already-migrated one every time after,
       so this has to be idempotent in both directions --
       :func:`towbook_agent.core.db.upgrade_to_head` is where that is handled.
       It is done here rather than in a release command because Railway has no
       release phase: if the web process does not migrate, nothing does.

    2. Start the scheduler **in this process**. The board is the only delivery
       mechanism now, so "the web service is up but nothing is refreshing it" is
       a silent failure that renders as confident, stale numbers. One service
       that both serves and schedules cannot get into that state. See
       :mod:`towbook_agent.core.scheduler` (``start_background_scheduler``) and
       :mod:`towbook_agent.core.leader` for the double-run guard.

    **A failure in either step does not stop the app from serving.** That is
    deliberate and it is the whole point: a container that exits on boot tells
    the owner nothing at all, whereas a container that comes up and puts a red
    banner across every tab tells him exactly what broke. Both outcomes are
    recorded in :data:`BOOT` and surfaced by ``/health``.
    """
    from ..core import db as core_db
    from ..core import scheduler as core_scheduler

    BOOT["storage_warning"] = core_db.warn_if_ephemeral_sqlite()
    BOOT["migration"] = core_db.upgrade_to_head()
    if not BOOT["migration"].get("ok"):
        logger.critical(
            "the database is NOT at the migration head: %s. The board will serve, but "
            "expect errors and check /health.",
            BOOT["migration"].get("error"),
        )

    # After the migration, because it writes a row into a table migration 0008
    # creates. Before the scheduler, because it is the difference between a
    # fresh deployment that is behind one account per customer and one that is
    # behind a shared password with access to every company on it.
    try:
        BOOT["accounts"] = accounts.bootstrap_operator_from_env()
        if BOOT["accounts"]:
            logger.warning("dashboard accounts: %s", BOOT["accounts"])
    except Exception as exc:  # pragma: no cover - never stop the app booting
        BOOT["accounts"] = None
        logger.error("could not create the operator account from the environment: %s", exc)

    BOOT["scheduler"] = core_scheduler.start_background_scheduler()
    try:
        yield
    finally:
        core_scheduler.stop_background_scheduler()


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
        lifespan=lifespan,
    )
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # The password gate wraps everything except /healthz, /login and /static.
    # Added here rather than per-route so a view added later is protected by
    # default -- a dashboard where forgetting a decorator publishes a customer's
    # numbers is a dashboard that will eventually publish a customer's numbers.
    application.add_middleware(PasswordGateMiddleware)
    # Only meaningful while the install is still in shared-password mode. Once
    # an account exists the shared password is refused outright, and warning
    # about a credential that no longer opens anything trains people to ignore
    # the line that matters. The check is wrapped because create_app runs
    # before the lifespan migration, so the table may not exist yet.
    try:
        shared_password_still_in_use = not accounts.accounts_are_configured()
    except Exception:  # pragma: no cover - the database is not up yet
        shared_password_still_in_use = True
    if shared_password_still_in_use and auth.password_is_default():
        # Said on the login page too, but the login page is only read by
        # somebody who was already going to sign in. This line is in the deploy
        # log, which is what the person who put it on a public URL is looking at.
        logger.warning(
            "DASHBOARD_PASSWORD is not set, so the board is protected by the "
            "default '%s'. That is four published digits guarding every client "
            "name, volume and acceptance rate on it -- and every company's, if "
            "this instance serves more than one. Set DASHBOARD_PASSWORD.",
            auth.DEFAULT_PASSWORD,
        )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters.update(
        {
            "pct": f_pct,
            "points": f_points,
            "num": f_num,
            "money": f_money,
            "money0": f_money0,
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
            "TABS": TABS,
            "DETAIL_NAV": DETAIL_NAV,
            "HOURLY_REFRESH_SECONDS": HOURLY_REFRESH_SECONDS,
            "basis_note": q.basis_note,
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
    response = templates.TemplateResponse(
        request=request, name=name, context=context, status_code=status_code
    )
    # The chosen company sticks. Every tab reads it back out of the cookie, so
    # switching once holds for the whole session instead of being lost on the
    # first internal link that does not carry the query string.
    return _remember_company(request, response, context.get("company_id"))


def _shell(request: HTTPRequest, active: str, **extra: Any) -> dict[str, Any]:
    """Context every full page needs: nav state, clock, staleness, empty state.

    ``pipeline_banner`` is part of the shell rather than of any one view on
    purpose. There is no SMS and no email any more -- config/notifications.yaml
    ships with every route disabled and this board is the delivery mechanism --
    so a failed or overdue run has to be impossible to miss from *whichever* tab
    the owner happens to open. See :func:`towbook_agent.web.queries.pipeline_banner`.
    """
    company_id = extra.pop("company", None) or _company(request)
    # EVERYTHING in the shell is scoped to the selected company, including the
    # staleness banner and the pipeline-failure banner. Those two are the whole
    # alarm system now that there is no SMS, and an alarm that reads another
    # tenant's healthy run is worse than no alarm.
    with _companies.use_company(company_id) as company_id:
        last_run = extra.pop("last_run", None)
        if last_run is None:
            last_run = q.last_run_summary(company_id)
        stale = bool(
            last_run
            and last_run.get("age_hours") is not None
            and last_run["age_hours"] > STALE_RUN_HOURS
        )
        companies = q.company_options()
        context: dict[str, Any] = {
            "active": active,
            "now": q.now_local(),
            "timezone": str(q.local_tz()),
            "thresholds": q.rate_thresholds(),
            "has_data": q.has_any_data(company_id),
            "last_run": last_run,
            "run_is_stale": stale,
            "pipeline_banner": q.pipeline_banner(company_id=company_id),
            "storage_warning": _storage_warning(),
            # The switcher. `companies` is EMPTY when only one is configured,
            # and base.html renders nothing at all in that case -- a dropdown
            # with one option is furniture on every page of the install this
            # system was built for.
            "companies": companies,
            "company": q.current_company(),
            "company_id": company_id,
            "multi_company": bool(companies),
            "notice": request.query_params.get("notice") or None,
            "notice_level": request.query_params.get("level") or "info",
            # WHO IS READING THIS PAGE. None only in shared-password mode,
            # where there is no account and therefore no name to show. The
            # header uses it for the sign-out label, the Accounts link (which
            # renders for an operator and for nobody else) and -- on a board
            # that now serves several towing companies -- for the plain
            # statement of whose numbers these are.
            "principal": (
                principal.as_dict()
                if (principal := auth.current_principal(request)) is not None
                else None
            ),
        }
        context.update(extra)
        return context


def _storage_warning() -> str | None:
    """"This deployment is about to lose all of its history", if that is true.

    A container filesystem is ephemeral. ``sqlite:///data/towbook.db`` is the
    default, so a Railway service with no DATABASE_URL set comes up perfectly
    healthy and silently wipes thirty days of offers on the next redeploy. The
    only moment anyone would notice is after it has already happened, so it is
    said here, on every page, while it is still fixable.
    """
    try:
        return q.core_db.warn_if_ephemeral_sqlite()
    except Exception:  # pragma: no cover - defensive; the board must render
        return None


#: Cookie that remembers which company the operator last looked at. Not a
#: secret and not a permission -- every company on this board belongs to the
#: same operator -- so it is a plain cookie rather than a signed session value.
#: It exists so that clicking through the tabs does not silently drop back to
#: the default company on the first link that forgets the query string.
COMPANY_COOKIE = "towbook_company"
COMPANY_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def _company(request: HTTPRequest) -> str:
    """Which company this request is about.

    ``?company=`` (or the old ``?account=``) wins, then the remembered cookie,
    then the roster's ``default_company``. An unknown id resolves to the
    default rather than 404ing, because a stale bookmark must not be a dead
    end -- ``core.companies.resolve_company`` logs it.
    """
    requested = (
        request.query_params.get("company")
        or request.query_params.get("account")
        or ""
    ).strip()
    if not requested:
        requested = (request.cookies.get(COMPANY_COOKIE) or "").strip()
    return _companies.resolve_company_id(requested or None)


def _remember_company(
    request: HTTPRequest, response: Any, company_id: str | None = None
) -> Any:
    """Persist the selected company on the response, when there is a choice.

    No cookie is written on a single-company install: there is nothing to
    remember, and a cookie nobody needs is a cookie banner nobody wants.
    """
    if not _companies.is_multi_company():
        return response
    resolved = company_id or _company(request)
    if request.cookies.get(COMPANY_COOKIE) != resolved:
        response.set_cookie(
            COMPANY_COOKIE,
            resolved,
            max_age=COMPANY_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
    return response


def _account(request: HTTPRequest) -> str:
    """Deprecated alias for :func:`_company`."""
    return _company(request)


def _api_company(request: HTTPRequest, company: str | None = None) -> str:
    """Which company a JSON endpoint is about. Same precedence as the tabs.

    ``?company=``, then the remembered cookie, then the roster default. The
    endpoints keep their declared ``company`` query parameter so it stays in the
    OpenAPI schema; this is what makes them fall back to the *cookie* rather
    than silently answering for the default company while the board is showing
    somebody else's.

    That gap was not theoretical. ``/api/clients/{slug}`` took no company
    argument at all, so a request for one tenant's client page was answered with
    the default tenant's history under the requested tenant's name.
    """
    explicit = (company or "").strip()
    if explicit:
        return _companies.resolve_company_id(explicit)
    return _company(request)


@app.get("/company")
def switch_company_form(request: HTTPRequest) -> RedirectResponse:
    """The no-JavaScript path of the switcher: ``/company?company=<id>``.

    The dropdown navigates directly when scripting is on; this is what its
    ``<noscript>`` submit button hits. Same validation, same cookie.
    """
    return switch_company(request, request.query_params.get("company") or "")


@app.get("/company/{company_id}")
def switch_company(request: HTTPRequest, company_id: str) -> RedirectResponse:
    """Select a company and go back where you were.

    A GET rather than a POST because it changes nothing but a preference, and
    because it has to work from a plain link when JavaScript is off. The
    redirect target is validated as a same-site path -- an open redirect on a
    board that is about to sit on a public URL is not worth the convenience.
    """
    resolved = _companies.resolve_company_id(company_id)
    target = (request.query_params.get("next") or "/").strip()
    if not target.startswith("/") or target.startswith("//"):
        target = "/"
    response = RedirectResponse(url=target, status_code=303)
    return _remember_company(request, response, resolved)


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
# Login -- the one pair of routes outside the password gate
# ==========================================================================


def _login_context(request: HTTPRequest, **extra: Any) -> dict[str, Any]:
    """Everything the login form needs, including WHICH form it is.

    ``accounts_mode`` decides between a username-and-password form and the
    single shared-password box this board started with. It is read from the
    database rather than from a setting, because the presence of an account IS
    the setting -- see web/accounts.py.
    """
    try:
        accounts_mode = accounts.accounts_are_configured()
    except Exception:  # pragma: no cover - the database is unreachable
        accounts_mode = False
    return {
        "next": auth.safe_next(request.query_params.get("next")),
        "accounts_mode": accounts_mode,
        # Only ever shown in shared-password mode: the default password stops
        # opening anything the moment an account exists.
        "password_is_default": auth.password_is_default() and not accounts_mode,
        "default_password": auth.DEFAULT_PASSWORD,
        "session_days": auth.session_max_age() // 86400,
        "session_secret_is_ephemeral": not (os.environ.get("SESSION_SECRET") or "").strip(),
        "APP_VERSION": __version__,
        "error": None,
        "username": "",
        **extra,
    }


@app.get("/login", response_class=HTMLResponse)
def view_login(request: HTTPRequest) -> HTMLResponse:
    """The login form. Exempt from the gate, for obvious reasons.

    An already-authenticated visitor is sent on rather than shown a form they
    do not need.
    """
    if auth.is_authenticated(request):
        return RedirectResponse(
            url=auth.safe_next(request.query_params.get("next")), status_code=303
        )
    return _render(request, "login.html", _login_context(request))


@app.post("/login")
def submit_login(
    request: HTTPRequest,
    password: str = Form(default=""),
    username: str = Form(default=""),
    next: str = Form(default="/"),
) -> Any:
    """Sign in, by account if this install has any and by shared password if not.

    ONE MESSAGE FOR EVERY KIND OF FAILURE. An unknown username, a disabled
    account and a wrong password all render "That username and password do not
    match an account here". Saying which of the three it was tells whoever is
    guessing which usernames are real, and that is the expensive half of the
    guessing.

    There is no lockout after repeated attempts, and saying so is better than
    implying otherwise. The honest mitigations are in place instead: PBKDF2 at
    600,000 iterations makes each guess cost real CPU, and a wrong username
    costs the same as a wrong password so the endpoint cannot be used to
    enumerate accounts. A counter would live in this process's memory and be
    lost on every redeploy, which is security theatre with a maintenance bill.
    """
    target = auth.safe_next(next)
    who = request.client.host if request.client else "?"

    try:
        accounts_mode = accounts.accounts_are_configured()
    except Exception:  # pragma: no cover - the database is unreachable
        accounts_mode = False

    if not accounts_mode:
        if not auth.verify_password(password):
            logger.warning("failed dashboard login from %s", who)
            return _render(
                request,
                "login.html",
                _login_context(request, error="That password is not correct.", next=target),
                status_code=401,
            )
        response = RedirectResponse(url=target, status_code=303)
        return auth.set_session_cookie(response, request)

    user = accounts.authenticate(username, password)
    if user is None:
        logger.warning(
            "failed dashboard sign-in for %r from %s",
            accounts.normalise_username(username),
            who,
        )
        return _render(
            request,
            "login.html",
            _login_context(
                request,
                error="That username and password do not match an account here.",
                next=target,
                username=accounts.normalise_username(username),
            ),
            status_code=401,
        )

    logger.info("dashboard sign-in: %r from %s", user.username, who)
    if user.must_change_password:
        target = auth.PASSWORD_CHANGE_PATH
    response = RedirectResponse(url=target, status_code=303)
    return auth.set_user_session_cookie(response, request, user)


@app.post("/logout")
def submit_logout(request: HTTPRequest) -> RedirectResponse:
    """Drop the session cookie. POST only, so a prefetched link cannot do it."""
    response = RedirectResponse(url="/login", status_code=303)
    return auth.clear_session_cookie(response, request)


# ==========================================================================
# The reader's own account
#
# Two pages, both about the person signed in rather than about the numbers.
# They exist because a first password is handed over by the operator -- read
# down a phone line, typed into an email -- and an account still carrying one
# is an account whose password is in somebody else's records.
# ==========================================================================


@app.get(auth.PASSWORD_CHANGE_PATH, response_class=HTMLResponse)
def view_password_change(request: HTTPRequest) -> HTMLResponse:
    """The change-your-password form.

    Reachable at any time from the header, and the ONLY page reachable while
    ``must_change_password`` is set -- the middleware redirects everything else
    here, so a new account cannot read a single figure until the password that
    travelled to it has been replaced.
    """
    principal = auth.current_principal(request)
    return _render(
        request,
        "password.html",
        _password_context(request, principal),
    )


def _password_context(
    request: HTTPRequest, principal: Any, **extra: Any
) -> dict[str, Any]:
    return {
        "APP_VERSION": __version__,
        "principal": principal.as_dict() if principal is not None else None,
        "forced": bool(principal is not None and principal.must_change_password),
        "min_length": accounts.MIN_PASSWORD_LENGTH,
        "error": None,
        "APP_TITLE": "Change your password",
        **extra,
    }


@app.post(auth.PASSWORD_CHANGE_PATH)
def submit_password_change(
    request: HTTPRequest,
    current_password: str = Form(default=""),
    new_password: str = Form(default=""),
    confirm_password: str = Form(default=""),
) -> Any:
    """Replace the signed-in reader's password.

    The current password is required even though the session already proves who
    this is: it is what stops an unattended browser from becoming a permanent
    handover of the account.

    On success every OTHER session this user has open dies too, because
    ``password_changed_at`` is part of the session signature. A fresh cookie is
    issued to the browser doing the changing, so the person who just chose a
    new password is not immediately thrown back to the login form.
    """
    principal = auth.current_principal(request)
    if principal is None or principal.user_id is None:
        # Shared-password mode has no account to change. It is not an error
        # worth a page of its own -- there is nothing here for that reader.
        return RedirectResponse(url="/", status_code=303)

    def fail(message: str) -> HTMLResponse:
        return _render(
            request,
            "password.html",
            _password_context(request, principal, error=message),
            status_code=400,
        )

    if accounts.authenticate(principal.username, current_password) is None:
        return fail("That is not your current password.")
    if new_password != confirm_password:
        return fail("The two new passwords do not match.")
    if new_password == current_password:
        return fail("That is the password you already have. Choose a different one.")

    try:
        accounts.set_password(principal.user_id, new_password, must_change=False)
    except accounts.AccountError as exc:
        return fail(str(exc))

    user = accounts.get_user(user_id=principal.user_id)
    response = RedirectResponse(
        url="/?notice=Your+password+has+been+changed.+Any+other+device+signed+in+as+you+has+been+signed+out.&level=info",
        status_code=303,
    )
    return auth.set_user_session_cookie(response, request, user)


# ==========================================================================
# Accounts -- operator only
#
# The screen where a towing company is given a login. Everything here is
# refused to a member: an account that can widen its own company scope is not
# a permission model, it is a suggestion.
# ==========================================================================


def _require_operator(request: HTTPRequest) -> Any:
    """The principal, if it may manage accounts. Otherwise raise a 404.

    404 and not 403, and deliberately: a member has no business knowing this
    screen exists, and "forbidden" confirms it does. The operator reaching it
    legitimately never sees either.
    """
    principal = auth.current_principal(request)
    if principal is None or not principal.may_manage_accounts():
        raise HTTPException(status_code=404, detail="Not found")
    return principal


def _accounts_context(request: HTTPRequest, **extra: Any) -> dict[str, Any]:
    users = accounts.list_users()
    return _shell(
        request,
        "accounts",
        users=[
            {
                "id": user.id,
                "username": user.username,
                "display_name": user.display_name,
                "email": user.email,
                "role": user.role,
                "is_operator": user.role == accounts.ROLE_OPERATOR,
                "company_scope": list(user.company_scope or []),
                "scope_label": accounts.scope_description(
                    accounts.principal_for_user(user)
                ),
                "enabled": user.enabled,
                "must_change_password": user.must_change_password,
                "last_login_at": user.last_login_at,
                "created_at": user.created_at,
            }
            for user in users
        ],
        # Every company on the install, because only an operator sees this page
        # and an operator is unscoped by definition.
        roster=[
            {"id": company.id, "label": company.label}
            for company in _companies.enabled_companies()
        ],
        min_length=accounts.MIN_PASSWORD_LENGTH,
        role_operator=accounts.ROLE_OPERATOR,
        role_member=accounts.ROLE_MEMBER,
        error=None,
        **extra,
    )


@app.get("/accounts", response_class=HTMLResponse)
def view_accounts(request: HTTPRequest) -> HTMLResponse:
    """Who can sign in, what they can see, and when they last did."""
    _require_operator(request)
    return _render(request, "accounts.html", _accounts_context(request))


@app.post("/accounts/create")
def submit_account_create(
    request: HTTPRequest,
    username: str = Form(default=""),
    display_name: str = Form(default=""),
    email: str = Form(default=""),
    password: str = Form(default=""),
    role: str = Form(default=accounts.ROLE_MEMBER),
    company_ids: list[str] = Form(default=[]),
) -> Any:
    """Create an account for one towing company.

    ``company_ids`` is a multi-select, so a customer who runs two entities gets
    both and one login. The new account is created with
    ``must_change_password``: the password chosen here is going to be read down
    a phone or pasted into an email, and it must not survive first use.

    THE ONE EXCEPTION is the account somebody creates for themselves out of the
    shared-password bootstrap. That password did not travel anywhere -- they
    typed it into this form and they are about to type it into the login form
    thirty seconds later -- so forcing a change would be a hoop for its own
    sake, and hoops for their own sake are how people learn to click through
    warnings.
    """
    operator = _require_operator(request)
    bootstrapping_self = operator.is_shared_password and role == accounts.ROLE_OPERATOR
    try:
        accounts.create_user(
            username,
            password,
            role=role,
            company_ids=company_ids,
            display_name=display_name,
            email=email,
            must_change_password=not bootstrapping_self,
        )
    except accounts.AccountError as exc:
        return _render(
            request, "accounts.html", _accounts_context(request, error=str(exc)), status_code=400
        )
    return RedirectResponse(
        url=f"/accounts?notice={quote(f'Created {accounts.normalise_username(username)}. Give them the password once; the board will make them change it before it shows them anything.')}",
        status_code=303,
    )


@app.post("/accounts/{user_id}/update")
def submit_account_update(
    request: HTTPRequest,
    user_id: int,
    display_name: str = Form(default=""),
    email: str = Form(default=""),
    role: str = Form(default=accounts.ROLE_MEMBER),
    company_ids: list[str] = Form(default=[]),
    enabled: str = Form(default=""),
) -> Any:
    """Change an account's companies, role or name, or disable it."""
    operator = _require_operator(request)
    if operator.user_id == user_id and role != accounts.ROLE_OPERATOR:
        # Demoting yourself is how an operator locks themselves out of the one
        # screen that could undo it. `update_user` guards the last-operator
        # case; this guards the more common single-click version of it.
        return _render(
            request,
            "accounts.html",
            _accounts_context(
                request,
                error=(
                    "You cannot take your own operator role away from this screen. "
                    "Make somebody else an operator first, then have them change you."
                ),
            ),
            status_code=400,
        )
    try:
        accounts.update_user(
            user_id,
            role=role,
            company_ids=company_ids,
            display_name=display_name,
            email=email,
            enabled=bool(enabled),
        )
    except accounts.AccountError as exc:
        return _render(
            request, "accounts.html", _accounts_context(request, error=str(exc)), status_code=400
        )
    return RedirectResponse(url="/accounts?notice=Saved.", status_code=303)


@app.post("/accounts/{user_id}/password")
def submit_account_password(
    request: HTTPRequest, user_id: int, password: str = Form(default="")
) -> Any:
    """Set a new password for somebody else -- the forgotten-password path.

    Always leaves ``must_change_password`` set, whoever it is for. An operator
    resetting a customer's password has, for that moment, a credential to that
    customer's board; forcing the change on first use is what closes that
    window without needing an email path this system does not have.
    """
    _require_operator(request)
    try:
        accounts.set_password(user_id, password, must_change=True)
    except accounts.AccountError as exc:
        return _render(
            request, "accounts.html", _accounts_context(request, error=str(exc)), status_code=400
        )
    return RedirectResponse(
        url="/accounts?notice=Password+reset.+They+will+be+asked+to+change+it+when+they+sign+in.",
        status_code=303,
    )


@app.post("/accounts/{user_id}/delete")
def submit_account_delete(request: HTTPRequest, user_id: int) -> Any:
    """Remove an account. Prefer disabling -- see ``accounts.delete_user``."""
    operator = _require_operator(request)
    if operator.user_id == user_id:
        return _render(
            request,
            "accounts.html",
            _accounts_context(request, error="You cannot delete the account you are signed in as."),
            status_code=400,
        )
    try:
        accounts.delete_user(user_id)
    except accounts.AccountError as exc:
        return _render(
            request, "accounts.html", _accounts_context(request, error=str(exc)), status_code=400
        )
    return RedirectResponse(url="/accounts?notice=Account+deleted.", status_code=303)


# ==========================================================================
# Tab 1 -- HOURLY. The board that replaced the hourly text message.
# ==========================================================================


@app.get("/hourly", response_class=HTMLResponse)
def view_hourly(request: HTTPRequest) -> HTMLResponse:
    """Today, hour by hour. The screen the hourly SMS used to be.

    Everything the message carried is here -- the hour line, the running day
    line, and the unanswered warning when there is one -- reproduced verbatim
    by the same notifier helpers that built the message, then expanded into the
    table around them. The body re-fetches itself every 60 seconds so a tab left
    open on a wall screen stays current without anybody reloading it.
    """
    company = _company(request)
    data = q.hourly_snapshot(company_id=company)
    return _render(
        request,
        "hourly.html",
        _shell(request, "hourly", hourly=data, company=company, last_run=data.get("last_run")),
    )


@app.get("/partials/hourly", response_class=HTMLResponse)
def partial_hourly(request: HTTPRequest) -> HTMLResponse:
    """Body of the Hourly tab, for the HTMX polling refresh."""
    company = _company(request)
    data = q.hourly_snapshot(company_id=company)
    return _render(
        request,
        "partials/hourly_body.html",
        {
            "hourly": data,
            "company_id": company,
            "thresholds": q.rate_thresholds(),
            "now": q.now_local(),
        },
    )


# ==========================================================================
# Tabs 2 and 3 -- WEEKLY and MONTHLY
# ==========================================================================


@app.get("/weekly", response_class=HTMLResponse)
def view_weekly(request: HTTPRequest) -> HTMLResponse:
    """This week against last: the coverage split, the causes, the fixes."""
    company = _company(request)
    data = q.period_snapshot("week", company_id=company)
    return _render(
        request,
        "weekly.html",
        _shell(request, "weekly", period=data, company=company),
    )


@app.get("/monthly", response_class=HTMLResponse)
def view_monthly(request: HTTPRequest) -> HTMLResponse:
    """This month against last: cause trend, client trajectories, close-offs."""
    company = _company(request)
    data = q.period_snapshot("month", company_id=company)
    return _render(
        request,
        "monthly.html",
        _shell(request, "monthly", period=data, company=company),
    )


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
    company = _company(request)
    days = _days(request)
    data = q.missed_work_snapshot(days=days, company_id=company)
    return _render(
        request,
        "missed.html",
        _shell(request, "missed", missed=data, company=company, day_options=DAY_OPTIONS),
    )


@app.get("/blind-spots", response_class=HTMLResponse)
def view_blind_spots(request: HTTPRequest) -> HTMLResponse:
    """When offers go unanswered, as a 7 x 24 hour-of-week grid.

    The staffing-decision view: it turns "we are missing work" into "nobody is
    covering these specific hours", which is an argument with evidence attached.
    """
    company = _company(request)
    days = _days(request)
    data = q.blind_spots_snapshot(days=days, company_id=company)
    return _render(
        request,
        "blind_spots.html",
        _shell(request, "blind_spots", spots=data, company=company, day_options=DAY_OPTIONS),
    )


@app.get("/revenue", response_class=HTMLResponse)
def view_revenue(request: HTTPRequest) -> HTMLResponse:
    """Lost revenue as a running total, and the hours that cost the most.

    Every other view counts jobs. This one multiplies them by the owner's own
    per-client average job values, so the question "what is this costing us"
    has a page instead of an arithmetic exercise. The number is an estimate and
    the page says so in three places -- Towbook publishes no offer amount, so
    there is no version of this figure that came out of the feed.
    """
    company = _company(request)
    days = _days(request, q.REVENUE_WINDOW_DAYS)
    data = q.revenue_snapshot(days=days, company_id=company)
    return _render(
        request,
        "revenue.html",
        _shell(request, "revenue", revenue=data, company=company, day_options=DAY_OPTIONS),
    )


@app.get("/close-off", response_class=HTMLResponse)
def view_closeoff(request: HTTPRequest) -> HTMLResponse:
    """Work we do not want, grouped by the client who keeps sending it.

    Grouped by client because the action is a conversation with that client, so
    each group carries a paste-ready summary of exactly that conversation.
    """
    company = _company(request)
    days = _days(request)
    data = q.closeoff_snapshot(days=days, company_id=company)
    return _render(
        request,
        "closeoff.html",
        _shell(request, "closeoff", closeoff=data, company=company, day_options=DAY_OPTIONS),
    )


# ==========================================================================
# View 1 -- Live
# ==========================================================================


@app.get("/live", response_class=HTMLResponse)
def view_live(request: HTTPRequest) -> HTMLResponse:
    company = _company(request)
    data = q.live_snapshot(company_id=company)
    return _render(
        request,
        "live.html",
        _shell(request, "live", live=data, company=company, last_run=data.get("last_run")),
    )


@app.get("/partials/live", response_class=HTMLResponse)
def partial_live(request: HTTPRequest) -> HTMLResponse:
    """Body of the Live view, for the HTMX polling refresh."""
    company = _company(request)
    data = q.live_snapshot(company_id=company)
    return _render(
        request,
        "partials/live_body.html",
        {
            "live": data,
            "company_id": company,
            "thresholds": q.rate_thresholds(),
            "now": q.now_local(),
        },
    )


# ==========================================================================
# View 2 -- Daily
# ==========================================================================


def _daily_context(request: HTTPRequest) -> dict[str, Any]:
    params = request.query_params
    company = _company(request)
    # Default to yesterday: the daily report is a post-mortem of a finished day.
    day = q.parse_date(params.get("date"), q.today_local() - timedelta(days=1))
    sort = params.get("sort") or "volume"
    direction = params.get("dir") or "desc"
    data = q.daily_snapshot(day, company_id=company, sort=sort, direction=direction)
    return {
        "daily": data,
        "company_id": company,
        "thresholds": q.rate_thresholds(),
        "has_data": q.has_any_data(company),
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
    company = _company(request)
    sort = params.get("sort") or "volume"
    direction = params.get("dir") or "desc"
    data = q.clients_overview(company_id=company, sort=sort, direction=direction)
    return {
        "overview": data,
        "company_id": company,
        "thresholds": q.rate_thresholds(),
        "has_data": q.has_any_data(company),
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
    company = _company(request)
    slug = (slug or "").strip("/") or q.EMPTY_CLIENT_KEY
    try:
        days = max(1, min(int(request.query_params.get("days") or 30), 365))
    except ValueError:
        days = 30
    data = q.client_detail(slug, days=days, company_id=company)
    return _render(
        request,
        "client_detail.html",
        _shell(request, "clients", client=data, company=company),
    )


# ==========================================================================
# View 4 -- Trends
# ==========================================================================


@app.get("/trends", response_class=HTMLResponse)
def view_trends(request: HTTPRequest) -> HTMLResponse:
    """The important-trends tab: the patterns no single period can show.

    Five things, in the order they answer "is this getting better or worse":
    the 7 x 24 blind-spot grid, the coverage gap week by week, client
    trajectories, offer volume, and the close-off candidates still arriving.

    The blind-spot grid and the close-off list are the *same* snapshots
    ``/blind-spots`` and ``/close-off`` render, over this tab's window. They
    are not recomputed a second way -- one of the two would eventually be wrong
    and nobody would be able to tell which.
    """
    company = _company(request)
    try:
        weeks = int(request.query_params.get("weeks") or 8)
    except ValueError:
        weeks = 8
    weeks = max(1, min(weeks, 26))
    data = q.trends_snapshot(weeks=weeks, company_id=company)
    days = min(weeks * 7, 365)
    return _render(
        request,
        "trends.html",
        _shell(
            request,
            "trends",
            trends=data,
            spots=q.blind_spots_snapshot(days=days, company_id=company),
            closeoff=q.closeoff_snapshot(days=days, company_id=company),
            company=company,
            week_options=(4, 8, 13, 26),
        ),
    )


# ==========================================================================
# Maps -- the offered heat map and the declined-jobs map
# ==========================================================================


@app.get("/maps", response_class=HTMLResponse)
def view_maps(request: HTTPRequest) -> HTMLResponse:
    """Two maps of the service market, over a day / week / month.

    MAP 1 is a heat map of where jobs were OFFERED -- where the work is, and the
    question the owner actually asked, where it is NOT. MAP 2 marks every job we
    did NOT accept; each popup carries the Towbook reference, the service, the
    time it was offered, whether it was actioned and by whom, and the decline
    reason. The light-service jobs among them are tallied at a flat per-job
    dollar value so the owner can see what he is choosing to give away.

    Daily is the default -- the board is opened through the day -- with a week
    and a month for review, and prev/next to walk the windows.
    """
    company = _company(request)
    scope = (request.query_params.get("scope") or q.DEFAULT_MAP_SCOPE).strip()
    if scope not in q.MAP_SCOPES:
        scope = q.DEFAULT_MAP_SCOPE
    # "Today" is a question about THIS company's clock -- resolve the default
    # anchor inside its timezone, exactly as the daily view does.
    with _companies.use_company(company):
        anchor = q.parse_date(request.query_params.get("date"), q.today_local())
    data = q.maps_snapshot(scope=scope, anchor=anchor, company_id=company)
    return _render(
        request,
        "maps.html",
        # `maps` drives the server-rendered stats and tables; `maps_js` is the
        # same data made JSON-safe (dates -> ISO strings) for the Leaflet map,
        # embedded in the page so the maps need no second round-trip to render.
        _shell(request, "maps", maps=data, maps_js=q.jsonable(data), company=company),
    )


# ==========================================================================
# View 5 -- Rules
# ==========================================================================


@app.get("/rules", response_class=HTMLResponse)
def view_rules(request: HTTPRequest) -> HTMLResponse:
    data = q.rules_view(company_id=_company(request))
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
    data = q.health_view(company_id=_company(request))
    return _render(request, "health.html", _shell(request, "health", health=data))


@app.get("/healthz")
def liveness() -> JSONResponse:
    """Machine-readable liveness probe. No secrets: the DB URL is masked.

    Railway's health check hits this, and :mod:`towbook_agent.web.auth` exempts
    it from the password gate so it can. It reports the two boot steps as well as
    the database, because "the container is running" and "the container migrated
    and is scheduling" are different questions and only the second one matters.

    It deliberately still returns 200 when the scheduler did not start: a board
    that serves stale numbers *and says so* is more useful than a deploy that
    fails its health check and rolls back to an equally broken previous version.

    THE SCHEDULER FIELD IS LIVE, NOT THE BOOT SNAPSHOT. It used to be read from
    ``BOOT["scheduler"]``, a dict written once during startup and never touched
    again -- so a process that acquired the scheduler lease *after* boot (see
    ``_watch_for_the_lease``) went on reporting ``running: false`` forever, and
    a process whose scheduler died later went on reporting ``running: true``.
    Both directions are wrong, and this endpoint is the one thing a person has
    to answer "is it actually scheduling" from outside the container. The boot
    snapshot is still reported, separately and honestly labelled, because "it
    never started" and "it started and stopped" need different fixes.
    """
    # Imported here, like the lifespan does, rather than at module scope.
    from ..core import scheduler as core_scheduler

    database = q.core_db.healthcheck()
    migration = BOOT.get("migration") or {}
    boot_scheduler = BOOT.get("scheduler") or {}
    try:
        scheduler = dict(core_scheduler.background_scheduler_status() or {})
        watcher_alive = core_scheduler.lease_watcher_alive()
    except Exception as exc:  # pragma: no cover - the probe must always answer
        scheduler = {"running": None, "jobs": 0, "error": f"{type(exc).__name__}: {exc}"}
        watcher_alive = None
    payload = {
        "ok": bool(database.get("ok")),
        "version": __version__,
        "database": {
            "ok": database.get("ok"),
            "backend": database.get("backend"),
            "tables": len(database.get("tables") or []),
        },
        "migration": {"ok": migration.get("ok"), "revision": migration.get("revision")},
        "scheduler": {
            "running": scheduler.get("running"),
            # background_scheduler_status() returns the job records; the probe
            # wants the count, as it always reported.
            "jobs": len(scheduler.get("jobs") or [])
            if isinstance(scheduler.get("jobs"), list)
            else scheduler.get("jobs"),
            "reason": scheduler.get("reason"),
            # Is the take-over thread still alive? Without this, "not running"
            # cannot be told apart from "not running and never going to be",
            # which is the difference between waiting and paging someone.
            "retry_watcher_alive": watcher_alive,
            "lease": scheduler.get("lease"),
            "last_error": scheduler.get("last_error"),
            "at_boot": {
                "running": boot_scheduler.get("running"),
                "reason": boot_scheduler.get("reason"),
            },
        },
        "storage_warning": BOOT.get("storage_warning"),
        # Roster-wide, not per company: this is a liveness probe, and "does
        # ANY company have data" is the question a deploy check is asking.
        "companies": len(_companies.enabled_companies()),
        "has_data": bool(q.companies_with_data()),
        "timezone": str(q.local_tz()),
    }
    return JSONResponse(payload, status_code=200 if payload["ok"] else 503)


# ==========================================================================
# JSON for the charts
# ==========================================================================


@app.get("/api/missed-work")
def api_missed_work(
    request: HTTPRequest,
    days: int = Query(default=q.MISSED_WORK_WINDOW_DAYS, ge=1, le=365),
    company: str | None = Query(default=None),
) -> JSONResponse:
    data = q.missed_work_snapshot(days=days, company_id=_api_company(request, company))
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
    request: HTTPRequest,
    days: int = Query(default=q.MISSED_WORK_WINDOW_DAYS, ge=1, le=365),
    company: str | None = Query(default=None),
) -> JSONResponse:
    data = q.blind_spots_snapshot(days=days, company_id=_api_company(request, company))
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
    request: HTTPRequest,
    days: int = Query(default=q.MISSED_WORK_WINDOW_DAYS, ge=1, le=365),
    company: str | None = Query(default=None),
) -> JSONResponse:
    data = q.closeoff_snapshot(days=days, company_id=_api_company(request, company))
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


@app.get("/api/companies")
def api_companies(request: HTTPRequest) -> JSONResponse:
    """Which companies the reader may open, and which one they are looking at.

    The scope, made visible. Everything else on this board enforces it
    silently, which is right for the product and useless for anybody trying to
    confirm the enforcement is real -- including the test suite, which asserts
    against this endpoint that one member's merged view covers their companies
    and not the install's.

    It reports the READER'S roster, not the server's: a member scoped to one
    company sees one entry here, and cannot learn from it that the install
    holds four others.
    """
    company_id = _company(request)
    principal = auth.current_principal(request)
    merged = (
        _companies.merged_company() if _companies.is_multi_company() else None
    )
    return JSONResponse(
        q.jsonable(
            {
                "company_id": company_id,
                "companies": [
                    {"id": company.id, "label": company.label}
                    for company in _companies.enabled_companies()
                ],
                "merged_available": merged is not None,
                "merged_members": list(merged.members) if merged is not None else [],
                "signed_in_as": principal.username if principal is not None else None,
                "is_operator": bool(principal is not None and principal.is_operator),
            }
        )
    )


@app.get("/api/hourly")
def api_hourly(
    request: HTTPRequest, company: str | None = Query(default=None)
) -> JSONResponse:
    """The hourly board as JSON, including the exact text the SMS carried.

    ``text_block`` is in the payload on purpose: this endpoint is the seam
    where somebody will eventually re-attach a message transport, and it should
    not have to be rebuilt from the parts to do it.
    """
    company_id = _api_company(request, company)
    data = q.hourly_snapshot(company_id=company_id)
    return JSONResponse(
        q.jsonable(
            {
                # WHICH COMPANY THIS IS ABOUT, stated in the payload rather
                # than assumed from the request. `?company=` is a request, not
                # an answer: it is resolved against the reader's own companies
                # and quietly falls back to one of theirs, so a caller that
                # echoed its own parameter back would be labelling one
                # company's figures with another company's name.
                "company_id": company_id,
                "date": data["date"],
                "generated_at": data["generated_at"],
                "current_hour": data["current_hour"],
                "current": data["current"],
                "hours": data["hours"],
                "totals": data["totals"],
                "text_block": data["text_block"],
                "day_alert_line": data["day_alert_line"],
                "basis_note": data["basis_note"],
                "has_data": data["has_data"],
                "has_today": data["has_today"],
            }
        )
    )


@app.get("/api/period")
def api_period(
    request: HTTPRequest,
    kind: str = Query(default="week"),
    company: str | None = Query(default=None),
) -> JSONResponse:
    """A period tab as JSON. ``kind`` is ``week`` or ``month``."""
    data = q.period_snapshot(kind, company_id=_api_company(request, company))
    return JSONResponse(
        q.jsonable(
            {
                "kind": data["kind"],
                "available": data["available"],
                "error": data["error"],
                "current_first": data["current_first"],
                "current_last": data["current_last"],
                "previous_first": data["previous_first"],
                "previous_last": data["previous_last"],
                "is_partial": data["is_partial"],
                "totals": data["totals"],
                "headline_buckets": data["headline_buckets"],
                "coverage": data["coverage"],
                "causes": data["causes"],
                "clients": data["clients"],
                "closeoff": data["closeoff"],
                "blind_spots": data["blind_spots"],
                "recommendations": data["recommendations"],
                "basis_note": data["basis_note"],
                "has_data": data["has_data"],
            }
        )
    )


@app.get("/api/live")
def api_live(
    request: HTTPRequest, company: str | None = Query(default=None)
) -> JSONResponse:
    data = q.live_snapshot(company_id=_api_company(request, company))
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
    request: HTTPRequest,
    date: str | None = Query(default=None),
    company: str | None = Query(default=None),
) -> JSONResponse:
    company_id = _api_company(request, company)
    # Inside the company, because "yesterday" is a question about its clock:
    # a Texas tenant and an Ohio one do not roll over at the same instant.
    with _companies.use_company(company_id):
        day = q.parse_date(date, q.today_local() - timedelta(days=1))
    data = q.daily_snapshot(day, company_id=company_id)
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
def api_clients(
    request: HTTPRequest, company: str | None = Query(default=None)
) -> JSONResponse:
    data = q.clients_overview(company_id=_api_company(request, company))
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
def api_client_detail(
    request: HTTPRequest,
    slug: str,
    days: int = Query(default=30, ge=1, le=365),
    company: str | None = Query(default=None),
) -> JSONResponse:
    """One client's history, as JSON.

    ``company`` is not optional decoration here. A client key is a casefolded
    name, and names are not unique across tenants -- two companies on this
    install can both be offered work by Agero. Without the scope this endpoint
    answered every request from the roster default, so asking for one tenant's
    Agero page returned another tenant's Agero rows. See
    ``tests/test_companies.py`` ->
    ``test_the_client_detail_json_endpoint_cannot_read_across_companies``.
    """
    data = q.client_detail(
        (slug or "").strip("/") or q.EMPTY_CLIENT_KEY,
        days=days,
        company_id=_api_company(request, company),
    )
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


@app.get("/api/maps")
def api_maps(
    request: HTTPRequest,
    scope: str = Query(default=q.DEFAULT_MAP_SCOPE),
    date: str | None = Query(default=None),
    company: str | None = Query(default=None),
) -> JSONResponse:
    """Both maps as JSON. ``scope`` is ``day`` / ``week`` / ``month``.

    The HTML view embeds this same data in the page, so nothing here is needed
    to render a map; the endpoint exists for parity with the other views and so
    the data behind a screenshot can be pulled and diffed.
    """
    company_id = _api_company(request, company)
    if scope not in q.MAP_SCOPES:
        scope = q.DEFAULT_MAP_SCOPE
    with _companies.use_company(company_id):
        anchor = q.parse_date(date, q.today_local())
    data = q.maps_snapshot(scope=scope, anchor=anchor, company_id=company_id)
    return JSONResponse(
        q.jsonable(
            {
                "scope": data["scope"],
                "first_day": data["first_day"],
                "last_day": data["last_day"],
                "label": data["label"],
                "offered": data["offered"],
                "declines": data["declines"],
                "light_service": data["light_service"],
                "geo_available": data["geo_available"],
                "has_data": data["has_data"],
            }
        )
    )


@app.get("/api/trends")
def api_trends(
    request: HTTPRequest,
    weeks: int = Query(default=8, ge=1, le=26),
    company: str | None = Query(default=None),
) -> JSONResponse:
    data = q.trends_snapshot(weeks=weeks, company_id=_api_company(request, company))
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
def api_health(
    request: HTTPRequest, company: str | None = Query(default=None)
) -> JSONResponse:
    data = q.health_view(company_id=_api_company(request, company))
    return JSONResponse(
        q.jsonable(
            {
                "database": {
                    "ok": data["database"].get("ok"),
                    "backend": data["database"].get("backend"),
                    "tables": data["database"].get("tables"),
                    "error": data["database"].get("error"),
                },
                # The board is the delivery mechanism, so "is anything scheduled
                # in this container" and "has a report gone quiet" are part of
                # health, not trivia.
                "scheduler": data.get("scheduler") or {},
                "overdue": [
                    {
                        "report_type": item.get("report_type"),
                        "overdue_seconds": item.get("overdue_seconds"),
                        "last_success": item.get("last_success"),
                    }
                    for item in (data.get("overdue") or [])
                ],
                "storage_warning": data.get("storage_warning"),
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


def resolve_port(explicit: int | None = None) -> int:
    """Which TCP port to listen on.

    ``PORT`` is checked before ``DASHBOARD_PORT`` because a container host
    assigns the port and injects it as ``PORT``; a service that ignores it binds
    somewhere the router is not looking and fails its health check with no
    useful error. ``DASHBOARD_PORT`` stays for local use.
    """
    for candidate in (explicit, os.environ.get("PORT"), os.environ.get("DASHBOARD_PORT")):
        if candidate in (None, ""):
            continue
        try:
            return int(candidate)
        except (TypeError, ValueError):
            logger.warning("ignoring non-numeric port %r", candidate)
    return 8080


def resolve_host(explicit: str | None = None) -> str:
    """Bind address. ``0.0.0.0`` inside a container, loopback on a laptop.

    Binding to 127.0.0.1 in a container means the platform's router cannot reach
    the process at all -- the deploy "succeeds" and every request 502s -- so a
    detected container host flips the default. It is only a default: ``--host``
    and ``DASHBOARD_HOST`` both still win.
    """
    if explicit:
        return explicit
    configured = (os.environ.get("DASHBOARD_HOST") or "").strip()
    if configured:
        return configured
    containerised = any(
        os.environ.get(name)
        for name in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RENDER", "FLY_APP_NAME", "DYNO")
    )
    return "0.0.0.0" if containerised else "127.0.0.1"


def serve(
    host: str | None = None,
    port: int | None = None,
    reload: bool = False,
    run_scheduler: bool | None = None,
) -> None:
    """Run the dashboard with uvicorn.

    ONE WORKER, DELIBERATELY. No ``workers=`` argument is passed and none should
    be added: the scheduler runs inside this process (see :func:`lifespan`), and
    N workers would be N schedulers. The advisory lock in
    :mod:`towbook_agent.core.leader` catches it on PostgreSQL, but the simplest
    correct configuration is the one where the situation never arises. This
    dashboard is a handful of people reading server-rendered pages; one worker is
    not the bottleneck and never will be.
    """
    import uvicorn

    if run_scheduler is not None:
        os.environ["RUN_SCHEDULER"] = "true" if run_scheduler else "false"

    host = resolve_host(host)
    port = resolve_port(port)
    logger.info("dashboard starting on http://%s:%s", host, port)
    if reload:
        uvicorn.run("towbook_agent.web.app:app", host=host, port=port, reload=True)
    else:
        uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    serve()
