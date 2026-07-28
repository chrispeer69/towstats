"""Command line entrypoint: ``python -m towbook_agent <command>``.

    run                 acquire -> ingest -> classify -> metrics -> analyst -> notify
    login-check         prove the Towbook credentials work. Run this first.
    discover-selectors  dump the live Request Log DOM to reconcile selectors.yaml
    backfill            re-derive service_class from the stored service_type_raw
    initdb              create the database and its tables
    seed                load fixture data end-to-end without touching the portal
    serve               run the dashboard
    schedule            run the APScheduler process

Global flags work on either side of the command, so both of these are valid::

    python -m towbook_agent --dry-run run --report daily --date 2026-07-27
    python -m towbook_agent run --report daily --date 2026-07-27 --dry-run

Exit codes -- ``run`` is wired into cron / Task Scheduler, so they matter:

    0   the report is trustworthy and was delivered
    1   a pipeline stage failed; a pipeline_failure alert was emitted
    2   bad usage (argparse)
    130 interrupted

Credentials are read from the environment only. ``.env`` is loaded when the
package is imported (see ``towbook_agent/__init__.py``); nothing is read from
source and nothing is written back.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from . import __version__, load_env

#: Must match ``core.models.DEFAULT_ACCOUNT_ID``. Duplicated as a literal so the
#: two diagnostic commands (login-check, discover-selectors) do not need
#: SQLAlchemy imported just to parse their arguments.
_DEFAULT_ACCOUNT = "default"

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

logger = logging.getLogger("towbook_agent.cli")


class UsageError(Exception):
    """A user-facing argument problem. Printed without a traceback, exit 2."""


# ==========================================================================
# Argument parsing
# ==========================================================================


def _add_global_flags(parser: argparse.ArgumentParser, suppress: bool) -> None:
    """Attach the global flags.

    Added twice: once to the root parser with real defaults, and once to a
    parent shared by every subparser with ``SUPPRESS`` defaults, so that a flag
    given before the command is not silently overwritten by the subparser's
    default.
    """

    def default(value: Any) -> Any:
        return argparse.SUPPRESS if suppress else value

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=default(False),
        help="do everything except send: messages are rendered and logged, not delivered",
    )
    parser.add_argument(
        "--log-level",
        type=lambda value: str(value).upper(),
        choices=_LOG_LEVELS,
        default=default(None),
        metavar="LEVEL",
        help="override the LOG_LEVEL environment variable",
    )
    parser.add_argument(
        "--account",
        default=default(None),
        metavar="ID",
        help=f"Towbook account id for multi-account setups (default: {_DEFAULT_ACCOUNT})",
    )
    parser.add_argument(
        "--no-acquire",
        action="store_true",
        default=default(False),
        help="do not open the portal; ingest the newest archived payload under raw/ instead",
    )
    parser.add_argument(
        "--xlsx",
        type=Path,
        default=default(None),
        metavar="PATH",
        help="ingest this specific payload (.json / .csv / .xlsx) instead of acquiring one",
    )
    parser.add_argument(
        "--source",
        choices=("api", "ui"),
        default=default(None),
        help=(
            "which acquisition path to use. api (default) = the JSON endpoint: no browser, "
            "and every row carries callRequestId, a real unique id. ui = Playwright + "
            "'Export to Excel', whose only id-shaped column is blank on exactly the "
            "unaccepted offers we measure. Applies to `run` and `login-check`."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="towbook-agent",
        description="Towbook Job Acceptance Intelligence System",
        epilog=(
            "Examples:\n"
            "  python -m towbook_agent login-check                 (JSON API path)\n"
            "  python -m towbook_agent login-check --source ui     (Playwright path)\n"
            "  python -m towbook_agent initdb\n"
            "  python -m towbook_agent seed\n"
            "  python -m towbook_agent run --report daily --date 2026-07-27\n"
            '  python -m towbook_agent run --report hourly --datetime "2026-07-27T14:00"\n'
            "  python -m towbook_agent run --report weekly --week-start 2026-07-20\n"
            "  python -m towbook_agent run --report monthly --month 2026-06\n"
            "  python -m towbook_agent schedule\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"towbook-agent {__version__}")
    _add_global_flags(parser, suppress=False)

    common = argparse.ArgumentParser(add_help=False)
    _add_global_flags(common, suppress=True)

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # -- run ---------------------------------------------------------------
    run_parser = subparsers.add_parser(
        "run",
        parents=[common],
        help="run the pipeline for one window",
        description=(
            "Run acquire -> ingest -> classify -> metrics -> analyst -> notify for one "
            "window. With no window flag the scheduled range for that report is used "
            "(hourly: the hour that just closed, daily: yesterday, weekly: the trailing "
            "7 days, monthly: the month that just closed). Exits non-zero if any "
            "required stage failed."
        ),
    )
    run_parser.add_argument(
        "--report",
        required=True,
        choices=("hourly", "daily", "weekly", "monthly"),
        help="which report to run",
    )
    run_parser.add_argument("--date", metavar="YYYY-MM-DD", help="calendar day for --report daily")
    run_parser.add_argument(
        "--datetime",
        dest="datetime",
        metavar="ISO",
        help='hour for --report hourly, e.g. "2026-07-27T14:00"',
    )
    run_parser.add_argument(
        "--week-start",
        dest="week_start",
        metavar="YYYY-MM-DD",
        help="Monday that starts the week for --report weekly",
    )
    run_parser.add_argument(
        "--month",
        dest="month",
        metavar="YYYY-MM",
        help=(
            "calendar month for --report monthly, e.g. 2026-06. "
            "A full date is accepted too and is snapped back to the 1st."
        ),
    )
    run_parser.add_argument(
        "--page-size", type=int, metavar="N", help="rows per portal page (default: schedule.yaml)"
    )
    run_parser.add_argument(
        "--no-notify",
        action="store_true",
        help="compute and store, but send nothing at all (unlike --dry-run, no message is rendered)",
    )
    run_parser.set_defaults(func=cmd_run)

    # -- login-check -------------------------------------------------------
    login_parser = subparsers.add_parser(
        "login-check",
        parents=[common],
        help="prove the Towbook credentials work",
        description=(
            "Log in to Towbook and report the outcome. No scraping, no database writes.\n"
            "\n"
            "--source api (the default) exercises the path a scheduled run actually uses: "
            "it authenticates with httpx, checks for the .xtl session cookie, and then "
            "fetches one row from /api/digitaldispatch/callrequests -- so a pass means "
            "'the pipeline can fetch data', not merely 'a cookie was issued'. It needs no "
            "browser and takes about a second.\n"
            "\n"
            "--source ui drives the Playwright login instead, and is what to run when the "
            "API check reports a bot wall or an MFA prompt, because it leaves a screenshot "
            "and an HTML dump behind."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    login_parser.set_defaults(func=cmd_login_check)

    # -- discover-selectors ------------------------------------------------
    discover_parser = subparsers.add_parser(
        "discover-selectors",
        parents=[common],
        help="dump the live Request Log DOM to reconcile config/selectors.yaml",
    )
    discover_parser.set_defaults(func=cmd_discover_selectors)

    # -- backfill ----------------------------------------------------------
    backfill_parser = subparsers.add_parser(
        "backfill",
        parents=[common],
        help="re-derive service_class from the stored service_type_raw",
        description=(
            "Re-classify every stored request against the current config/rules.yaml. "
            "service_type_raw is never modified. Run this after editing service_classes."
        ),
    )
    backfill_parser.set_defaults(func=cmd_backfill)

    # -- initdb ------------------------------------------------------------
    initdb_parser = subparsers.add_parser(
        "initdb", parents=[common], help="create the database and its tables (idempotent)"
    )
    initdb_parser.set_defaults(func=cmd_initdb)

    # -- seed --------------------------------------------------------------
    seed_parser = subparsers.add_parser(
        "seed",
        parents=[common],
        help="load fixture data end-to-end without touching the portal",
        description=(
            "Ingest a fixture XLSX and run the hourly, daily and weekly pipelines "
            "against it. Always a dry run: nothing is ever sent. Use --xlsx to supply "
            "your own fixture; otherwise tests/fixtures/*.xlsx is used, and if there is "
            "none a deterministic fixture is generated from config/schema.yaml."
        ),
    )
    seed_parser.add_argument(
        "--days", type=int, default=8, metavar="N", help="days of synthetic history to generate"
    )
    seed_parser.add_argument(
        "--regenerate",
        action="store_true",
        help="regenerate the synthetic fixture even if one already exists",
    )
    seed_parser.set_defaults(func=cmd_seed)

    # -- serve -------------------------------------------------------------
    serve_parser = subparsers.add_parser(
        "serve", parents=[common], help="run the dashboard"
    )
    serve_parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    serve_parser.add_argument(
        "--port", type=int, default=None, help="port (default: DASHBOARD_PORT, or 8080)"
    )
    serve_parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    serve_parser.set_defaults(func=cmd_serve)

    # -- schedule ----------------------------------------------------------
    schedule_parser = subparsers.add_parser(
        "schedule",
        parents=[common],
        help="run the APScheduler process",
        description=(
            "Run every job in config/schedule.yaml. The file is watched, so an edit "
            "rebuilds the job table without a restart. A watchdog emits pipeline_failure "
            "if a report type stops producing successful runs."
        ),
    )
    schedule_parser.set_defaults(func=cmd_schedule)

    return parser


# ==========================================================================
# Small shared helpers
# ==========================================================================


def _account(args: argparse.Namespace) -> str:
    return (getattr(args, "account", None) or _DEFAULT_ACCOUNT).strip() or _DEFAULT_ACCOUNT


def _flag(args: argparse.Namespace, name: str, default: Any = None) -> Any:
    return getattr(args, name, default)


_CLI_DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y",
    "%m/%d/%y %H:%M",
    "%m/%d/%y",
)


def _parse_local(value: str, tz) -> datetime:
    """Parse a CLI date/datetime into a timezone-aware local datetime."""
    text = str(value).strip()
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in _CLI_DATETIME_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise UsageError(
            f"could not read {value!r} as a date or time. "
            'Try 2026-07-27 or "2026-07-27T14:00".'
        )
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


_MONTH_RE = re.compile(r"^(\d{4})[-/](\d{1,2})$")


def _parse_month(value: str, tz) -> datetime:
    """Parse ``2026-06`` -- or any full date -- into local midnight on that day.

    ``YYYY-MM`` is the form the owner will type and the only one
    :func:`_parse_local` cannot read: ``datetime.fromisoformat("2026-06")``
    raises, and every fallback format expects a day. Handled here rather than by
    adding ``%Y-%m`` to _CLI_DATETIME_FORMATS, because that list is shared with
    --date and --datetime, where a bare month is not a legal window.
    """
    text = str(value).strip()
    match = _MONTH_RE.match(text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if not 1 <= month <= 12:
            raise UsageError(f"{value!r} is not a month: {month} is not between 1 and 12")
        return datetime(year, month, 1, tzinfo=tz)
    return _parse_local(text, tz)


def _resolve_window(report_type: str, args: argparse.Namespace, tz) -> tuple[datetime, datetime, str]:
    """Work out which window ``run`` should process, and say where it came from."""
    from .core.scheduler import DEFAULT_RANGE, load_job_specs, resolve_range, window_for

    supplied = [
        (flag, value)
        for flag, value in (
            ("--datetime", _flag(args, "datetime")),
            ("--date", _flag(args, "date")),
            ("--week-start", _flag(args, "week_start")),
            ("--month", _flag(args, "month")),
        )
        if value
    ]
    if len(supplied) > 1:
        names = ", ".join(flag for flag, _ in supplied)
        raise UsageError(f"give at most one window flag; got {names}")

    if not supplied:
        range_name = DEFAULT_RANGE[report_type]
        try:
            for spec in load_job_specs(strict=False):
                if spec.report_type == report_type:
                    range_name = spec.range_name
                    break
        except Exception:  # pragma: no cover - config errors surface elsewhere
            pass
        start, end = resolve_range(range_name)
        return start, end, f"scheduled range {range_name}"

    flag, value = supplied[0]

    if report_type == "hourly":
        if flag != "--datetime":
            raise UsageError(
                f'--report hourly takes --datetime, not {flag}. '
                'Example: --report hourly --datetime "2026-07-27T14:00"'
            )
        anchor = _parse_local(value, tz)
        start, end = window_for("hourly", anchor)
        return start, end, f"explicit hour {start:%Y-%m-%d %H:%M}"

    if report_type == "daily":
        if flag != "--date":
            raise UsageError(
                f"--report daily takes --date, not {flag}. "
                "Example: --report daily --date 2026-07-27"
            )
        anchor = _parse_local(value, tz)
        start, end = window_for("daily", anchor)
        return start, end, f"explicit day {start:%Y-%m-%d}"

    if report_type == "monthly":
        # --date is accepted as an alias: any day in the month works, because
        # window_for and run_pipeline both snap back to the 1st
        # (metrics_monthly.month_start is always a 1st).
        if flag not in ("--month", "--date"):
            raise UsageError(
                f"--report monthly takes --month, not {flag}. "
                "Example: --report monthly --month 2026-06"
            )
        anchor = _parse_month(value, tz)
        start, end = window_for("monthly", anchor)
        if anchor.day != 1:
            origin = f"{flag} {anchor:%Y-%m-%d} snapped back to the 1st"
        else:
            origin = f"explicit month {start:%Y-%m}"
        return start, end, origin

    # weekly. --date is accepted as an alias for --week-start: any day in the
    # week works, because run_pipeline snaps the window back to its Monday
    # (metrics_weekly.week_start is always a Monday).
    if flag not in ("--week-start", "--date"):
        raise UsageError(
            f"--report weekly takes --week-start, not {flag}. "
            "Example: --report weekly --week-start 2026-07-20"
        )
    anchor = _parse_local(value, tz)
    start, end = window_for("weekly", anchor)
    if anchor.weekday() != 0:
        origin = f"{flag} {anchor:%Y-%m-%d} snapped back to Monday"
    else:
        origin = f"explicit week starting {anchor:%Y-%m-%d}"
    return start, end, origin


def _print_result(result: Any, window_origin: str) -> None:
    """Print a human-readable summary of a PipelineResult."""
    from .core.scheduler import summarize_metrics, timezone_name

    offered, accepted, rate = summarize_metrics(result.metrics)
    rate_text = "--" if rate is None else f"{round(rate * 100):d}%"

    print()
    print(
        f"{result.report_type.upper()}  "
        f"{result.window_start:%Y-%m-%d %H:%M} -> {result.window_end:%Y-%m-%d %H:%M} "
        f"({timezone_name()})"
    )
    print(f"  window     {window_origin}")
    print(f"  run_id     {result.run_id}")
    print(f"  account    {result.account_id}")
    print(f"  rules      {result.rules_version}")
    print(f"  source     {getattr(result, 'source', '?')}")
    if result.xlsx_path:
        print(f"  payload    {result.xlsx_path}")
    print(f"  rows       {result.row_count}")
    print(f"  offered    {offered}    accepted {accepted}    rate {rate_text}")
    if result.alerts:
        names = ", ".join(sorted({str(a.get('alert_id')) for a in result.alerts}))
        print(f"  alerts     {len(result.alerts)} fired: {names}")
    for warning in result.warnings:
        print(f"  warning    {warning}")
    for error in result.errors:
        print(f"  ERROR      {error}")
    if result.dry_run:
        print("  delivery   DRY RUN - nothing was sent")
    print(f"  status     {result.status.upper()}  ({result.duration_seconds:.1f}s)")
    print()


# ==========================================================================
# run
# ==========================================================================


def cmd_run(args: argparse.Namespace) -> int:
    from .core.scheduler import local_timezone, newest_archived_xlsx, run_pipeline

    tz = local_timezone()
    report_type = args.report
    start, end, origin = _resolve_window(report_type, args, tz)

    xlsx = _flag(args, "xlsx")
    no_acquire = bool(_flag(args, "no_acquire", False))

    if xlsx is not None:
        xlsx = Path(xlsx)
        if not xlsx.is_file():
            raise UsageError(f"--xlsx {xlsx} does not exist")
    elif no_acquire:
        newest = newest_archived_xlsx()
        if newest is None:
            raise UsageError(
                "--no-acquire was given but there is no archived XLSX under raw/. "
                "Run without --no-acquire, or pass --xlsx PATH."
            )
        xlsx = newest

    result = run_pipeline(
        report_type,
        start,
        end,
        page_size=_flag(args, "page_size"),
        account_id=_account(args),
        dry_run=bool(_flag(args, "dry_run", False)),
        acquire=not (no_acquire or xlsx is not None),
        xlsx_path=xlsx,
        notify=not bool(_flag(args, "no_notify", False)),
        # None means "whatever config/schedule.yaml says for this report",
        # which defaults to the JSON API.
        source=_flag(args, "source"),
    )
    _print_result(result, origin)
    return result.exit_code


# ==========================================================================
# login-check
# ==========================================================================

# Keyed by the outcome name that agents.acquisition.login_check() reports in
# result["stage"]. The bare aliases ("captcha", "network", ...) are kept so a
# future acquisition implementation using shorter names still resolves.
_LOGIN_REASONS: dict[str, str] = {
    "missing_credentials": "TOWBOOK_USER and TOWBOOK_PASS are not set in the environment.",
    "credentials_missing": "TOWBOOK_USER and TOWBOOK_PASS are not set in the environment.",
    "bad_credentials": "Towbook rejected the username or password.",
    "invalid_credentials": "Towbook rejected the username or password.",
    "mfa_required": "Towbook asked for a second factor, which an automated login cannot supply.",
    "captcha_or_bot_wall": "Towbook presented a CAPTCHA or bot challenge on the login page.",
    "captcha": "Towbook presented a bot challenge on the login page.",
    "locked_out": "The Towbook account is locked out.",
    "account_chooser": "Login succeeded but Towbook asked which account to use.",
    "network_error": "Could not reach Towbook. Check the network and TOWBOOK_BASE_URL.",
    "network": "Could not reach Towbook. Check the network and TOWBOOK_BASE_URL.",
    "timeout": "Towbook did not respond in time.",
    "selector_not_found": "The login form did not match config/selectors.yaml.",
    "navigation_failed": "The password was accepted but the expected page never loaded.",
    "unknown_post_login_state": (
        "The password was accepted but the page that loaded was not recognised."
    ),
    "environment_error": "Playwright or its browser is not installed.",
    "playwright_missing": "Playwright or its browser is not installed.",
    "unknown": "Login did not complete, and Towbook gave no recognisable reason.",
}

_LOGIN_NEXT_STEPS: dict[str, str] = {
    "missing_credentials": "Copy .env.example to .env and fill in TOWBOOK_USER and TOWBOOK_PASS.",
    "credentials_missing": "Copy .env.example to .env and fill in TOWBOOK_USER and TOWBOOK_PASS.",
    "bad_credentials": "Check TOWBOOK_USER / TOWBOOK_PASS in .env, then run login-check again.",
    "invalid_credentials": "Check TOWBOOK_USER / TOWBOOK_PASS in .env, then run login-check again.",
    "mfa_required": (
        "Run: python -m towbook_agent.agents.acquisition bootstrap-session "
        "(headed, you finish the second factor by hand and the session is saved)."
    ),
    "captcha_or_bot_wall": "Set HEADLESS=false and run login-check again to see what Towbook is showing.",
    "captcha": "Set HEADLESS=false and run login-check again to see what Towbook is showing.",
    "locked_out": "Log in manually at https://app.towbook.com/Security/Login to clear the lockout.",
    "selector_not_found": "Run: python -m towbook_agent discover-selectors, then reconcile config/selectors.yaml.",
    "navigation_failed": "Run: python -m towbook_agent discover-selectors to see where it landed.",
    "unknown_post_login_state": "Run: python -m towbook_agent discover-selectors to see where it landed.",
    "environment_error": "Run: python -m playwright install chromium",
    "playwright_missing": "Run: python -m playwright install chromium",
    "timeout": "Set HEADLESS=false and PLAYWRIGHT_SLOWMO=300, then run login-check again.",
    "network_error": "Check connectivity to app.towbook.com, and any proxy or firewall in the way.",
    "network": "Check connectivity to app.towbook.com, and any proxy or firewall in the way.",
}


def _pick(source: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = source.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def cmd_login_check(args: argparse.Namespace) -> int:
    """The first thing the owner runs. Human-readable and decisive."""
    base_url = (os.environ.get("TOWBOOK_BASE_URL") or "https://app.towbook.com").strip()
    user = (os.environ.get("TOWBOOK_USER") or "").strip()
    password = os.environ.get("TOWBOOK_PASS") or ""

    if not user or not password:
        absent = [
            name
            for name, present in (("TOWBOOK_USER", bool(user)), ("TOWBOOK_PASS", bool(password)))
            if not present
        ]
        verb = "is" if len(absent) == 1 else "are"
        print(
            f"LOGIN FAILED [missing_credentials] - "
            f"{' and '.join(absent)} {verb} not set in the environment."
        )
        print(f"  next step:   {_LOGIN_NEXT_STEPS['missing_credentials']}")
        return 1

    # Default to the API path: it is what a scheduled run uses, it needs no
    # browser, and it proves the endpoint answers rather than only that a
    # cookie was issued.
    source = (_flag(args, "source") or "api").strip().lower()

    try:
        from .core.scheduler import _load_agent

        if source == "api":
            acquisition = _load_agent("acquisition_api")
            raw = acquisition.login_check_api(account_id=_account(args))
        else:
            acquisition = _load_agent("acquisition")
            raw = acquisition.login_check(account_id=_account(args))
    except Exception as exc:
        print(f"LOGIN FAILED [error] - login_check itself raised {type(exc).__name__}: {exc}")
        logger.debug("login_check raised", exc_info=True)
        print("  next step:   run with --log-level DEBUG for the full traceback.")
        return 1

    result: dict[str, Any] = raw if isinstance(raw, dict) else {"ok": bool(raw)}

    ok = bool(_pick(result, "ok", "success", "authenticated", "logged_in", default=False))
    screenshot = _pick(result, "screenshot", "screenshot_path", "png", "image")
    detail = _pick(result, "message", "detail", "error", "note")

    if ok:
        who = _pick(result, "user", "username", "account", "display_name", default=user)
        where = _pick(result, "landing_url", "url", "final_url", "base_url", default=base_url)
        session_saved = bool(
            _pick(
                result,
                "storage_state_saved",
                "session_saved",
                "storage_state",
                "session_path",
                "state_path",
                default=False,
            )
        )
        suffix = "  (session saved)" if session_saved else ""
        print(f"LOGIN OK [{source}] - authenticated as {who} at {where}{suffix}")
        probe = (result.get("details") or {}).get("callrequests_probe")
        if isinstance(probe, dict) and probe.get("ok"):
            total = probe.get("x_records_count")
            print(
                "  endpoint:    /api/digitaldispatch/callrequests answered"
                + (f" ({total} record(s) available today)" if total is not None else "")
            )
    else:
        # acquisition.login_check() reports the named outcome as "stage"
        # (mirrored to "outcome"). Look there FIRST -- without it every failure
        # prints [unknown] and the operator gets no next step.
        reason = str(
            _pick(result, "stage", "outcome", "reason", "failure_reason", "code", "error_code",
                  default="unknown")
        )
        reason_key = reason.strip().lower().replace(" ", "_").replace("-", "_")
        explanation = _LOGIN_REASONS.get(reason_key) or (detail or _LOGIN_REASONS["unknown"])
        print(f"LOGIN FAILED [{source}/{reason_key}] - {explanation}")

    if screenshot:
        print(f"  screenshot:  {screenshot}")
    cookies = _pick(result, "cookies", "cookie_names", "session_cookies")
    if isinstance(cookies, (list, tuple)) and cookies:
        names = [c if isinstance(c, str) else str(c.get("name", c)) for c in cookies]
        print(f"  cookies:     {', '.join(names[:6])}")
    elapsed = _pick(result, "elapsed_ms", "duration_ms")
    if isinstance(elapsed, (int, float)):
        print(f"  elapsed:     {elapsed / 1000:.1f}s")
    for extra in ("dump", "html_path", "storage_state", "session_path"):
        value = result.get(extra)
        if value:
            print(f"  {extra + ':':<12} {value}")

    if ok:
        return 0

    if detail:
        print(f"  detail:      {detail}")
    reason = str(
        _pick(result, "stage", "outcome", "reason", "failure_reason", "code", default="unknown")
    )
    reason_key = reason.strip().lower().replace(" ", "_").replace("-", "_")
    next_step = _LOGIN_NEXT_STEPS.get(reason_key)
    if next_step:
        print(f"  next step:   {next_step}")
    if source == "api" and reason_key not in {"bad_credentials", "credentials_missing",
                                              "missing_credentials", "invalid_credentials"}:
        # A credential problem fails identically on both paths, so only suggest
        # the fallback when the failure could plausibly be endpoint-specific.
        print(
            "  fallback:    python -m towbook_agent login-check --source ui   "
            "(drives a real browser and leaves a screenshot + HTML dump behind)"
        )
    return 1


# ==========================================================================
# discover-selectors
# ==========================================================================


def cmd_discover_selectors(args: argparse.Namespace) -> int:
    from .core.paths import CONFIG_DIR
    from .core.scheduler import _load_agent

    acquisition = _load_agent("acquisition")
    destination = acquisition.discover_selectors(account_id=_account(args))
    print(f"Discovery dump written to {destination}")
    print(
        f"  Reconcile the candidates in {CONFIG_DIR / 'selectors.yaml'} against what was "
        "actually on the page."
    )
    print("  Selectors are a list of candidates tried in order; put the confirmed one first.")
    return 0


# ==========================================================================
# backfill
# ==========================================================================


def cmd_backfill(args: argparse.Namespace) -> int:
    from .core.config_loader import rules_version
    from .core.db import get_session, init_db
    from .core.scheduler import _load_agent

    dry_run = bool(_flag(args, "dry_run", False))
    init_db()
    classifier = _load_agent("classifier")

    with get_session(commit=not dry_run) as session:
        count = classifier.backfill(session)

    suffix = "  (dry run - nothing was committed)" if dry_run else ""
    print(f"Reclassified {count} request(s) against rules {rules_version()}.{suffix}")
    print("  service_type_raw was not modified; only service_class was re-derived.")
    return 0


# ==========================================================================
# initdb
# ==========================================================================


def cmd_initdb(args: argparse.Namespace) -> int:
    from .core.db import healthcheck, init_db, sqlite_file

    init_db()
    info = healthcheck()
    if not info["ok"]:
        print(f"DATABASE FAILED - {info['error']}")
        return 1

    print(f"Database ready: {info['url']}")
    path = sqlite_file()
    if path:
        size = path.stat().st_size if path.exists() else 0
        print(f"  file:    {path} ({size:,} bytes)")
    tables = [name for name in info["tables"] if not name.startswith("sqlite_")]
    print(f"  tables:  {', '.join(tables) if tables else '(none)'}")
    return 0


# ==========================================================================
# seed
# ==========================================================================

# Deterministic so that re-running `seed` regenerates a byte-identical fixture
# and therefore the same numbers -- hard constraint #4 applies to the seed path
# as much as to the scheduled one.
_SEED_RANDOM_SEED = 20260728

_SEED_CLIENTS = (
    "Agero",
    "Quest Roadside",
    "Allied Dispatch",
    "Geico Roadside",
    "Honk Technologies",
    "Urgently",
)

# Chosen so that every branch of the shipped rules.yaml is exercised, including
# two strings that match nothing, so `unclassified_service_type` has something
# to fire on.
_SEED_SERVICES = (
    "Light Duty Tow",
    "Flatbed Tow - Accident",
    "Heavy Duty Recovery",
    "Wrecker Tow",
    "Impound Tow",
    "Winch Out",
    "Winch-Out / Extraction",
    "Vehicle Stuck In Ditch",
    "Tire Change",
    "Flat Tire - Spare On Board",
    "Jump Start",
    "Battery Service",
    "Lockout",
    "Fuel Delivery",
    "Out Of Gas",
    "Mobile Diagnostic",
    "Storage Release",
)

_SEED_DENIALS = (
    "No truck available",
    "Too far outside coverage area",
    "Rate too low for the distance",
    "No driver on shift",
    "Wrong equipment for the vehicle",
    "",
)

_SEED_TRUCKS = ("Unit 7", "Unit 12", "Unit 3", "Unit 21")
_SEED_DRIVERS = ("R. Alvarez", "T. Boyd", "M. Chen", "D. Okafor")


def _seed_headers() -> list[tuple[str, str]]:
    """(canonical field, source header) pairs, taken from config/schema.yaml.

    The generated fixture uses the first candidate header for each canonical
    field, so it exercises the real header-mapping path in the ingester rather
    than a shortcut. Change schema.yaml and the fixture follows.
    """
    from .core.config_loader import get_schema

    schema = get_schema()
    columns = schema.get("columns") or {}
    order = (
        "request_id",
        "client_name",
        "offered_at",
        "responded_at",
        "status",
        "denial_reason",
        "service_type_raw",
        "pickup_location",
        "dropoff_location",
        "truck_assigned",
        "driver_assigned",
        "amount",
    )
    headers: list[tuple[str, str]] = []
    for field_name in order:
        candidates = columns.get(field_name) or []
        if isinstance(candidates, str):
            candidates = [candidates]
        if candidates:
            headers.append((field_name, str(candidates[0])))
    return headers


def _generate_seed_xlsx(destination: Path, days: int) -> Path:
    """Write a deterministic fixture XLSX covering the last ``days`` days."""
    import random

    from openpyxl import Workbook

    from .core.scheduler import now_local

    rng = random.Random(_SEED_RANDOM_SEED)
    headers = _seed_headers()

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Digital Requests"
    sheet.append([header for _, header in headers])

    now = now_local().replace(tzinfo=None)
    start = (now - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)

    rows: list[dict[str, Any]] = []
    moment = start
    index = 0
    while moment < now:
        index += 1
        service = rng.choice(_SEED_SERVICES)
        is_light = any(
            token in service.lower()
            for token in ("tire", "jump", "battery", "lockout", "fuel", "gas")
        )
        # Light service is declined far more often than a tow -- that asymmetry
        # is the whole point of the acceptance report.
        accept_probability = 0.25 if is_light else 0.82
        roll = rng.random()
        if roll < accept_probability:
            status = "Accepted"
        elif roll < accept_probability + 0.10:
            status = "Expired"
        elif roll < accept_probability + 0.14:
            status = "Canceled"
        else:
            status = "Denied"

        responded = moment + timedelta(seconds=rng.randint(20, 900))
        rows.append(
            {
                "request_id": f"DR{200000 + index}",
                "client_name": rng.choice(_SEED_CLIENTS),
                "offered_at": moment,
                "responded_at": responded if status != "Expired" else None,
                "status": status,
                "denial_reason": rng.choice(_SEED_DENIALS) if status == "Denied" else "",
                "service_type_raw": service,
                "pickup_location": f"{rng.randint(100, 9999)} {rng.choice(('Gratiot', 'Woodward', 'Van Dyke', 'Telegraph'))} Ave, Detroit MI",
                "dropoff_location": f"{rng.randint(100, 9999)} {rng.choice(('Main', 'Maple', 'Oak', 'Grand River'))} St, Detroit MI",
                "truck_assigned": rng.choice(_SEED_TRUCKS) if status == "Accepted" else "",
                "driver_assigned": rng.choice(_SEED_DRIVERS) if status == "Accepted" else "",
                "amount": round(rng.uniform(85, 640), 2) if status == "Accepted" else None,
            }
        )
        moment += timedelta(minutes=rng.randint(12, 70))

    # Guarantee the previous full hour has data, so `seed` always produces a
    # meaningful hourly report rather than an empty one.
    previous_hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    for offset in (7, 23, 41, 52):
        index += 1
        rows.append(
            {
                "request_id": f"DR{200000 + index}",
                "client_name": rng.choice(_SEED_CLIENTS),
                "offered_at": previous_hour + timedelta(minutes=offset),
                "responded_at": previous_hour + timedelta(minutes=offset, seconds=90),
                "status": "Accepted" if offset % 2 else "Denied",
                "denial_reason": "" if offset % 2 else "No truck available",
                "service_type_raw": "Light Duty Tow" if offset % 2 else "Tire Change",
                "pickup_location": "1 Woodward Ave, Detroit MI",
                "dropoff_location": "500 Main St, Detroit MI",
                "truck_assigned": "Unit 7" if offset % 2 else "",
                "driver_assigned": "R. Alvarez" if offset % 2 else "",
                "amount": 195.00 if offset % 2 else None,
            }
        )

    rows.sort(key=lambda row: row["offered_at"])
    for row in rows:
        sheet.append([row.get(field_name) for field_name, _ in headers])

    destination.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(destination)
    return destination


def _seed_fixture(args: argparse.Namespace) -> Path:
    """Find or build the fixture XLSX that ``seed`` should ingest."""
    from .core.paths import REPO_ROOT, STATE_DIR

    supplied = _flag(args, "xlsx")
    if supplied is not None:
        path = Path(supplied)
        if not path.is_file():
            raise UsageError(f"--xlsx {path} does not exist")
        return path

    if not _flag(args, "regenerate", False):
        fixtures_dir = REPO_ROOT / "tests" / "fixtures"
        if fixtures_dir.is_dir():
            candidates = [
                item
                for item in sorted(fixtures_dir.glob("*.xlsx"))
                if item.is_file() and not item.name.startswith("~$")
            ]
            if candidates:
                return max(candidates, key=lambda p: p.stat().st_mtime)

    destination = STATE_DIR / "seed" / "seed_requests.xlsx"
    if destination.is_file() and not _flag(args, "regenerate", False):
        return destination

    print(f"Generating a deterministic fixture at {destination} ...")
    return _generate_seed_xlsx(destination, max(1, int(_flag(args, "days", 8) or 8)))


def cmd_seed(args: argparse.Namespace) -> int:
    """Load fixture data through the real pipeline. Always a dry run."""
    from .core.db import init_db
    from .core.scheduler import (
        previous_calendar_day,
        previous_full_hour,
        now_local,
        run_pipeline,
        trailing_7_days,
    )

    init_db()
    fixture = _seed_fixture(args)
    print(f"Seeding from {fixture}")
    print("  seed is always a dry run: no SMS and no email will be sent.\n")

    account = _account(args)
    now = now_local()
    plan = (
        ("hourly", previous_full_hour(now), False),
        ("daily", previous_calendar_day(now), True),
        ("weekly", trailing_7_days(now), True),
    )

    worst = 0
    for report_type, (start, end), skip_ingest in plan:
        result = run_pipeline(
            report_type,
            start,
            end,
            account_id=account,
            dry_run=True,           # non-negotiable for seed
            acquire=False,
            xlsx_path=None if skip_ingest else fixture,
            skip_ingest=skip_ingest,
            notify=True,
        )
        _print_result(result, "seed fixture")
        worst = max(worst, result.exit_code)

    if worst == 0:
        print("Seed complete. Try: python -m towbook_agent serve")
    return worst


# ==========================================================================
# serve
# ==========================================================================


def cmd_serve(args: argparse.Namespace) -> int:
    import importlib.util

    from .core.db import init_db

    module_name = "towbook_agent.web.app"
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        print(f"Dashboard not available: {module_name} does not exist yet.")
        return 1

    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. Run: pip install -r requirements.txt")
        return 1

    init_db()

    port = args.port or int((os.environ.get("DASHBOARD_PORT") or "8080").strip() or 8080)
    host = args.host
    level = (_flag(args, "log_level") or os.environ.get("LOG_LEVEL") or "INFO").lower()

    print(f"Dashboard on http://{host}:{port}  (Ctrl-C to stop)")
    uvicorn.run(
        f"{module_name}:app",
        host=host,
        port=port,
        reload=bool(getattr(args, "reload", False)),
        log_level=level if level in {"critical", "error", "warning", "info", "debug", "trace"} else "info",
    )
    return 0


# ==========================================================================
# schedule
# ==========================================================================


def cmd_schedule(args: argparse.Namespace) -> int:
    from .core.scheduler import load_job_specs, run_scheduler, timezone_name

    dry_run = bool(_flag(args, "dry_run", False))
    specs = load_job_specs(strict=False)

    print(f"Scheduler starting in {timezone_name()}.")
    if dry_run:
        print("  DRY RUN - jobs will run, but nothing will be sent.")
    if not specs:
        print("  WARNING: config/schedule.yaml defines no runnable jobs.")
    for spec in specs:
        state = "" if spec.enabled else "  [disabled]"
        print(
            f"  {spec.name:<10} cron {spec.cron:<14} range {spec.range_name:<22} "
            f"report {spec.report_type}{state}"
        )
    print("  Edit config/schedule.yaml at any time; the job table rebuilds without a restart.")
    print("  Ctrl-C to stop.\n")

    return run_scheduler(dry_run=dry_run)


# ==========================================================================
# Entrypoint
# ==========================================================================


def main(argv: Sequence[str] | None = None) -> int:
    from .core.logging_setup import setup_logging

    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    # Credentials come from the environment only. Already loaded at package
    # import; called again so an explicitly exported value still wins and so a
    # .env created since import is picked up.
    load_env()
    setup_logging(level=_flag(args, "log_level"), force=True)

    try:
        return int(args.func(args) or 0)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        logger.critical("%s failed: %s", args.command, exc)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
