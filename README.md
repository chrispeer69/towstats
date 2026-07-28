# Towbook Job Acceptance Intelligence System

Watches the Towbook **Request Log → Digital Dispatches** grid, records every job
offer, classifies it against rules that live in YAML, computes acceptance
metrics, and sends the hourly / daily / weekly reports a human used to text by
hand.

The number this system exists to get right is **acceptance rate = accepted /
offered**. Every design decision below follows from protecting that denominator.

---

## Quick start

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium

copy .env.example .env          # then fill in TOWBOOK_USER / TOWBOOK_PASS

python -m towbook_agent login-check    # prove the credentials work FIRST
python -m towbook_agent initdb
python -m towbook_agent seed           # end-to-end on fixture data, sends nothing
python -m towbook_agent serve          # dashboard on http://127.0.0.1:8080
```

Use `.venv\Scripts\python.exe` if you are working in the checked-in virtualenv;
the system Python has none of the dependencies.

---

## Commands

| Command | What it does |
| --- | --- |
| `run --report {hourly\|daily\|weekly}` | acquire → ingest → classify → metrics → analyst → notify for one window |
| `login-check` | Log in and report the outcome. No scraping, no writes. Run this first. |
| `discover-selectors` | Dump the live Request Log DOM so `config/selectors.yaml` can be reconciled |
| `backfill` | Re-derive `service_class` from the stored `service_type_raw` |
| `initdb` | Create the database and its tables (idempotent) |
| `seed` | Load fixture data through the real pipeline. Always a dry run. |
| `serve` | Run the dashboard |
| `schedule` | Run the APScheduler process |

Window selection for `run`:

```bash
python -m towbook_agent run --report daily  --date 2026-07-27
python -m towbook_agent run --report hourly --datetime "2026-07-27T14:00"
python -m towbook_agent run --report weekly --week-start 2026-07-20
```

With no window flag, `run` uses the same `range:` keyword `config/schedule.yaml`
gives that report, so the manual and scheduled paths cannot drift apart.

Global flags work on either side of the command: `--dry-run`, `--log-level`,
`--account`, `--no-acquire`, `--xlsx PATH`.

Exit codes: `0` the report is trustworthy and was delivered · `1` a stage failed
and a `pipeline_failure` was emitted · `2` bad usage · `130` interrupted.

---

## Configuration is data, not code

Adding a service type, an acceptance policy, an alert, a notification route or a
schedule entry is a **YAML edit with no code change and no redeploy**. The
config loader re-stats each file on every access, so a running scheduler picks
up an edit on its next job.

| File | Owns |
| --- | --- |
| `config/rules.yaml` | Service classes, acceptance policy, alert expressions |
| `config/schema.yaml` | Export → canonical field mapping, status vocabulary, file format |
| `config/selectors.yaml` | Every DOM selector. There are **zero** selectors in Python. |
| `config/notifications.yaml` | Routes, recipients (env var *names*), quiet hours, rate limits, message templates |
| `config/schedule.yaml` | The cron jobs |
| `config/rules.proposed.yaml` | The Analyst's suggestions. Never auto-applied. |

`rules_version()` stamps every metrics row and every run with
`<version>-<sha256[:12]>` of `rules.yaml`, so any stored number can be traced
back to the rules that produced it.

### Alert expressions are sandboxed

`when:` expressions are evaluated by an AST whitelist in `core/safe_eval.py` —
no `eval()`, no builtins, no attribute access, no calls. An unsafe expression is
skipped with a warning rather than executed.

---

## The Towbook export: what is actually true

These facts were verified against the live portal and contradict Towbook's own
documentation. They are encoded in `config/schema.yaml`.

* **"Export to Excel" delivers a CSV**, not an XLSX — comma separated, CRLF,
  with **two preamble lines** above the header row. Ingestion reads both formats
  and *scans* for the header row rather than trusting a fixed offset.
* **The real columns** are `Request Date, Provider, Contractor ID, Service
  Needed, Call #, PO #, Expiration Date, Status, Response Reason, Responded by,
  ETA, Vehicle`. There is no `ID` column and no `Date and Time` column.
* **There is no request identifier.** `config/schema.yaml → identity` therefore
  fingerprints each row from its offer-time content. `Call #` is deliberately
  *not* part of the identity: it is blank on every offer that was never accepted
  and is filled in later, so keying on it would give one offer two keys across
  two exports and count it twice.
* The portal filters **by date only** and exports **only the current page**, so
  `page_size` must exceed a day's volume. Truncation is detected and raises a
  `pipeline_failure` rather than silently under-reporting.
* The session cookie is `.xtl`, not `.AspNetCore.Cookies`.

Run `discover-selectors` after any portal change; drifted headers abort the run,
write `config/schema.detected.yaml`, and store nothing.

---

## Guarantees

1. **Credentials come from the environment only.** Never in source, never
   committed, and scrubbed from every log record by a logging filter.
2. **Exactly one component uses an LLM** — the Analyst. Acquisition, ingestion,
   classification and metrics are fully deterministic.
3. **Every scheduled job is idempotent.** Requests upsert on `request_id`; each
   metrics table has a unique key on its window column. Re-running a window
   yields a byte-identical result.
4. **`service_type_raw` is immutable.** Classification only ever writes
   `service_class`, re-derived from the untouched source string, so a rules
   change reclassifies history without a migration.
5. **Silence is never success.** A failed stage emits a `pipeline_failure` that
   ignores quiet hours and rate limits, and a watchdog alerts if a report type
   stops producing successful runs at all.
6. **The Analyst cannot change the rules.** It may only append to
   `rules.proposed.yaml`; applying a proposal is a human click on `/rules`.

### Zero-offer windows

A window with no offers has an acceptance rate of `None`, not `0%`. Reporting
0% would tell the owner he turned down work that was never sent to him. The
dashboard renders it as an em-dash throughout.

---

## Dashboard

| Route | View |
| --- | --- |
| `/` | Live — today's running totals, hourly bars, running rate vs 7d/30d baselines |
| `/daily` | Yesterday's full breakdown, by client and service class |
| `/clients` | One row per client: 24h / 7d / 30d, sparkline, denial mix |
| `/trends` | Hour-of-week heatmap, client trajectories, volume |
| `/rules` | Current rules, proposed changes, unclassified backlog |
| `/health` | Run history, last success per type, recomputed-vs-stored metrics |

The dashboard **recomputes from the `requests` rows on every load** and shows
the stored metric beside it. `metrics_daily` is what the 06:00 SMS quoted and
must not be retro-edited, so `/health` makes a stale aggregate visible instead
of silently picking a winner.

Chart.js and HTMX are vendored in `towbook_agent/web/static/` — no CDN, no build
step. Both files carry a header with their download URL; replacing them with the
real libraries is a drop-in.

---

## Layout

```
towbook_agent/
  agents/    acquisition, ingestion, classifier, metrics, analyst, notifier
  core/      paths, config_loader, safe_eval, logging_setup, models, db, events, scheduler
  web/       app, queries, rules_admin, templates/, static/
config/      the six YAML files
raw/         archived exports, YYYY/MM/DD/
data/        the SQLite database
state/       logs, sessions, discovery dumps, event journal
tests/       pytest suite and the deterministic fixture generator
alembic/     migrations
```

Everything resolves through `core/paths.py`; nothing builds a path from the
current working directory. `TOWBOOK_REPO_ROOT` relocates the whole tree, which
is how the test suite sandboxes itself.

---

## Tests

```bash
python -m pytest
```

The suite sandboxes `TOWBOOK_REPO_ROOT` to a temp directory before the package
is imported, blocks the socket layer, and clears every credential, so it never
touches the real repo, the real database, or the network.
