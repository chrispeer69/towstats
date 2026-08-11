"""The 6 AM job: pull yesterday from Towbook, then write the printable report.

Invoked by Windows Task Scheduler through scripts/morning_report.cmd.

THE ORDERING RULE
    Pull first, report second, and write the report EVEN IF THE PULL FAILED.
    A missing file at 6 AM is indistinguishable from a quiet night; a report
    that prints with a red "the pull failed" banner tells the owner exactly
    which of the two happened. So the pull's exit status is captured and
    handed to the reader, never used to abort the report.

EXIT CODES
    0  report written (the pull may still have failed -- read the banner)
    1  report could not be written at all
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from towbook_agent.agents.morning_report import generate  # noqa: E402

def desktop_dir() -> Path:
    """The Desktop Explorer actually shows.

    NOT Path.home()/"Desktop". When OneDrive backs up the desktop it redirects
    the known folder to ~/OneDrive/Desktop and leaves the old ~/Desktop in
    place, empty. This machine is redirected, so the naive path resolves to a
    real directory that the user never sees -- the report would be written
    every morning, successfully, into a folder nobody opens.
    """
    try:
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            raw, _ = winreg.QueryValueEx(handle, "Desktop")
        resolved = Path(os.path.expandvars(raw))
        if resolved.is_dir():
            return resolved
    except (ImportError, OSError):
        pass

    for candidate in (Path.home() / "OneDrive" / "Desktop", Path.home() / "Desktop"):
        if candidate.is_dir():
            return candidate
    return Path.home() / "Desktop"


DEFAULT_DB = REPO / "data" / "towbook.db"
LOG_DIR = REPO / "state" / "logs"

#: The pull is a network round trip against a third-party portal. Capped so a
#: hung request cannot leave the task running until the next morning's.
PULL_TIMEOUT_SECONDS = 900


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / "morning_report.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # Logging must never be the reason the report does not get written.


def pull(day: date, *, python: str) -> tuple[bool, str]:
    """Run the existing daily pipeline for one day. Returns (ok, detail).

    --no-notify because this job's output is the .docx on disk. The email and
    SMS channels stay owned by the scheduler in schedule.yaml; firing them
    from here too would double every alert.
    """
    cmd = [
        python, "-m", "towbook_agent", "run",
        "--report", "daily",
        "--date", day.isoformat(),
        "--no-notify",
    ]
    log(f"pull: {' '.join(cmd[1:])}")
    try:
        proc = subprocess.run(
            cmd, cwd=str(REPO), capture_output=True, text=True,
            timeout=PULL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {PULL_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return False, f"could not start: {exc}"

    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    detail = tail[-1] if tail else f"exit {proc.returncode}"
    if proc.returncode != 0:
        log(f"pull FAILED (exit {proc.returncode}): {detail}")
        return False, detail
    log("pull ok")
    return True, detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", metavar="YYYY-MM-DD",
                        help="day to report on (default: yesterday, local)")
    parser.add_argument("--out", type=Path, default=None,
                        help="report root (default: <Desktop>\\Tow Reports)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--tz", default="America/Detroit")
    parser.add_argument("--no-pull", action="store_true",
                        help="skip the Towbook pull and report on the database as it stands")
    parser.add_argument("--open", dest="open_after", action="store_true",
                        help="open the report in Word when done (for manual runs)")
    args = parser.parse_args(argv)

    out_root = args.out or (desktop_dir() / "Tow Reports")
    tz = ZoneInfo(args.tz)
    target = (
        date.fromisoformat(args.date) if args.date
        else datetime.now(tz).date() - timedelta(days=1)
    )
    log(f"--- morning report for {target} ---")

    if not args.no_pull:
        pull(target, python=sys.executable)
    else:
        log("pull skipped (--no-pull)")

    if not args.db.exists():
        log(f"FATAL: no database at {args.db}")
        return 1

    try:
        path = generate(args.db, out_root, day=target, tz_name=args.tz)
    except Exception as exc:  # noqa: BLE001 -- the task must report, not crash silently
        log(f"FATAL: report generation failed: {exc!r}")
        return 1

    log(f"wrote {path}")
    if args.open_after:
        try:
            import os
            os.startfile(str(path))  # noqa: S606 -- Windows shell open, manual runs only
        except Exception as exc:  # noqa: BLE001
            log(f"could not open the file: {exc!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
