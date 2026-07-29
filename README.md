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
| `migrate` | `alembic upgrade head`. The deploy step; idempotent on an empty, current or unstamped database |
| `seed` | Load fixture data through the real pipeline. Always a dry run. |
| `serve` | Run the dashboard **and, by default, the scheduled jobs** (`--no-scheduler` to split them) |
| `schedule` | Run the APScheduler process on its own |

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
| `config/companies.yaml` | The tenant roster — one entry per towing company. Credential **variable names**, never credentials |
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

**The board is the delivery mechanism.** Nothing is texted and nothing is
emailed; the owner opens a URL several times a day. Anything a message used to
carry has to be on a screen here or it is gone.

Top-level navigation is four tabs:

| Tab | What it answers |
| --- | --- |
| `/hourly` | **Today, hour by hour.** Offers, accepted, unanswered, running day total, with the current hour shown large. This replaces the hourly SMS outright: the exact two-to-three lines that text carried are reproduced verbatim at the top of the page, built by the same `agents/notifier.py` helpers that built the message. The body re-fetches itself every 60 seconds, so a tab left open stays current. |
| `/weekly` | **This week against last.** The covered-vs-uncovered split, the cause behind each missed job and whether it is growing, and a ranked list of what to do about it. |
| `/monthly` | **This month against last.** Trend per cause, per-client trajectories, and whether the close-offs actually worked. |
| `/trends` | **The important trends.** The 7 × 24 blind-spot grid, the coverage gap week by week, client rate trajectories, offer volume, and the close-off candidates still arriving. |

Every comparison on the weekly and monthly tabs is **like-for-like**: on a
Tuesday, "this week" is two days old, so it is set against the *first two days*
of last week. The full previous period is shown beside it, labelled, never
instead of it. A period tab that reported a 70% collapse in volume every Monday
morning would be ignored by Tuesday.

The detail views the tabs summarise are one click away in the second navigation
row, each on the URL it has always had:

| Route | View |
| --- | --- |
| `/` | Missed work — the inventory of what we did not get, by cause, with the fix |
| `/blind-spots` | The 7 × 24 grid on its own, with the staffing argument |
| `/close-off` | Work we do not want, grouped by the client to have the conversation with |
| `/live` | Today's running totals, hourly bars, running rate vs 7d/30d baselines |
| `/daily` | Yesterday's full breakdown, by client and service class |
| `/clients` | One row per client: 24h / 7d / 30d, sparkline, denial mix, no-response rate |
| `/rules` | Current rules, proposed changes, unclassified backlog |
| `/health` | Run history, last success per type, recomputed-vs-stored metrics |

The dashboard **recomputes from the `requests` rows on every load** and shows
the stored metric beside it. `metrics_daily` is what the 06:00 report quoted and
must not be retro-edited, so `/health` makes a stale aggregate visible instead
of silently picking a winner.

Every ranked list on every tab states what unit it is in, via
`agents/notifier.py → ranking_note()` — the same helper the reports use, so the
board and a report cannot make different claims about the same figures. A rate
over zero offers renders as an em-dash, never `0%`. A brand-new deployment with
no data renders an empty state on every route rather than a 500.

Chart.js and HTMX are vendored in `towbook_agent/web/static/` — no CDN, no build
step. Both files carry a header with their download URL; replacing them with the
real libraries is a drop-in.

### The board is password protected — and "1234" is not enough

The whole dashboard sits behind a **single shared password** with a signed
session cookie (`towbook_agent/web/auth.py`, stdlib `hmac` — no new dependency).

| Variable | Default | What it does |
| --- | --- | --- |
| `DASHBOARD_PASSWORD` | `1234` | The shared password. Works out of the box, unset. |
| `SESSION_SECRET` | generated at boot | Signs the session cookie. |
| `DASHBOARD_SESSION_DAYS` | `30` | How long a login lasts. |

`/healthz` is exempt so Railway's health check passes without credentials.
Changing `DASHBOARD_PASSWORD` invalidates every outstanding session immediately.
If `SESSION_SECRET` is unset the app generates one at boot and **logs a warning**:
sessions will not survive a restart or redeploy, and two replicas would reject
each other's cookies.

> **Say this out loud, because it is the part that matters.**
>
> **`1234` is not adequate protection for real customer data.** It is four
> digits, it is published in this README, and it is the same on every deployment
> of this repository. There is one shared password here — no individual
> accounts, no record of who looked at what, and no lockout after repeated
> guesses — so anyone who learns it has every client name, every offer volume
> and every acceptance rate on the board. **If this instance serves more than
> one towing company, it has all of theirs too, and none of them agreed to
> share a password with the others.**
>
> Set `DASHBOARD_PASSWORD` to something long and random before the board holds
> data anybody would mind losing. On Railway: *Variables → New Variable*, then
> redeploy. The login page prints this same warning on screen for as long as the
> default is still in place.
>
> If several companies are ever going to use one deployment, a shared password
> is the wrong shape entirely and should be replaced with per-company logins.
> That is a larger change than this one and is deliberately not pretended to be
> done.

---

## Multiple companies

This repo is given and sold to other US Tow Alliance towing companies, so one
install reports on several of them out of one database. That is **multi-company
reporting, not a SaaS** — there is no billing, no signup and no user management,
because one operator runs the install and every company on it is theirs.

### The roster

`config/companies.yaml` lists the tenants. Delete the file and the system runs
as a single company whose id is `default` — exactly what it did before the file
existed, and exactly what every already-stored row is keyed on.

```yaml
default_company: default

companies:
  - id: roadside-towing               # STORED ON EVERY ROW. Never change it.
    name: Roadside Towing and Recovery Inc
    towbook_company_id: 61343
    credentials_env: ROADSIDE         # -> TOWBOOK_ROADSIDE_USER / _PASS
    timezone: America/Detroit
    enabled: true
    coverage: {...}                   # this company's staffed hours
    job_value_by_client: {...}        # this company's average job values
    rules: {...}                      # anything else in rules.yaml
```

**Credentials are never in this file.** It is committed to git and carries only
the *name* of the environment variable prefix. A company that declares
`credentials_env: ACME` reads `TOWBOOK_ACME_USER` / `TOWBOOK_ACME_PASS`, and it
**never falls back** to the plain `TOWBOOK_USER` pair if those are missing —
falling back would sign in as a different company and file its jobs under this
one. A company with no prefix uses `TOWBOOK_USER` / `TOWBOOK_PASS`, which is
what keeps an existing single-company install working with no edit at all.
Every `TOWBOOK_*_PASS` is scrubbed from log records by the same filter that
already handled `TOWBOOK_PASS`.

### Override precedence

`config/rules.yaml` is the default for every company. Later wins:

1. **`config/rules.yaml`** — the global default
2. **`coverage:`** → replaces `missed_work.coverage`
3. **`job_value_by_client:`** → replaces `missed_work.job_value_by_client`
4. **`rules:`** → deep-merged over everything above

2 and 3 **replace** the global block outright rather than merging into it. A
company that lists two clients' job values must not silently inherit the other
three from `rules.yaml` and price work it has never been offered, and a company
that declares `start: "12:00"` must not keep the global `days` and `end` — the
coverage contrast is the headline of every report and it is wrong if the window
is half somebody else's. 4 merges mapping by mapping; a **list or a scalar
replaces**, because `match_any` and `coverage.windows` are ordered decision
tables and appending to them would change which rule fires first.

A company in Texas is staffed different hours than one in Ohio. That is the
override that matters most, and it is why the coverage block is a first-class
key rather than something buried in `rules:`.

### What is separated, and how

`company_id` is a column on **every** table, and it leads **every** metrics
unique key — `metrics_daily(company_id, date)`, not `metrics_daily(date)`.
Without that, the second company to compute Tuesday overwrites the first one's
Tuesday with a plausible-looking number and nothing on the board looks wrong.
Every query in `web/queries.py`, `agents/metrics.py` and `agents/missed_work.py`
filters on it; `company_id=None` means *the company currently being computed*,
never *every company*. `tests/test_companies.py` seeds two companies with
deliberately different clients and asserts, by name rather than by count, that
no view returns the other's rows.

### The scheduler

A job in `config/schedule.yaml` that names no company runs **every enabled
company**, so adding a tenant is an edit to `companies.yaml` and never to the
cron table. Add `company_id: acme-towing` to pin one job to one company — useful
when a tenant in another timezone needs its own hours.

**One company's failure never stops the others.** Each company gets its own try
block, its own `pipeline_failure` event and its own `runs` row; the loop
continues. A tenant whose Towbook password expired cannot take the rest of the
roster's reporting down with it, and the failure shows up on that company's
own Health page rather than as an absence everywhere.

### The board

The company switcher appears in the header **only when more than one company is
enabled** — a dropdown with one option is furniture on every page of a
single-company install. The selection persists in a cookie so it survives the
next click on any tab, `?company=<id>` overrides it for one request, and every
tab, partial and `/api/...` endpoint respects it. `--company` does the same on
the CLI (`--account` is the old spelling and still works).

---

## Deploying to Railway

The board is the delivery mechanism: no SMS, no email, just a URL opened in a
browser several times a day. That changes what a deployment has to guarantee —
**if the scheduler stops, the board keeps rendering yesterday's numbers as
though they were today's, and nothing tells anyone.** Everything below follows
from that.

### Before you start: SQLite will destroy your data here

`DATABASE_URL` defaults to `sqlite:///data/towbook.db`. On a laptop that is
correct. **On Railway it is a data-loss bug that produces no error.**

A Railway container's filesystem is *ephemeral*: it is rebuilt from the image on
every redeploy, every restart, and every crash. `data/towbook.db` lives inside
it. So a service deployed without `DATABASE_URL` comes up perfectly, collects a
month of offers, shows correct numbers all week — and then silently resets to an
empty database the next time anybody pushes a commit. Nothing fails, nothing
logs an exception, and the only symptom is a board that has forgotten the month.

**Postgres is not optional on Railway.** The app detects this situation
(`RAILWAY_ENVIRONMENT` set, backend is SQLite), logs it at CRITICAL, and puts a
red banner across every tab — but do not rely on catching it after the fact.

### 1. Create the project

```bash
# Push this repo to GitHub first (github.com/<you>/towstats), then:
#   Railway -> New Project -> Deploy from GitHub repo -> pick it
```

`railway.json` and `nixpacks.toml` are read from the repo, so the build command,
start command, Python version and health check are already configured. Nothing
needs setting in the Railway UI except the variables in step 3.

### 2. Add PostgreSQL

```
Railway project -> New -> Database -> Add PostgreSQL
```

Then, **on the app service** (not the database), add the reference variable:

```
DATABASE_URL = ${{Postgres.DATABASE_URL}}
```

The `${{Postgres.DATABASE_URL}}` form is a Railway reference — it stays correct
if the database is ever recreated. Railway hands out `postgresql://…`; older
plugins hand out `postgres://…`, which SQLAlchemy 2 rejects outright. Both are
rewritten to `postgresql+psycopg://` by `core/db.py`, so paste whichever you are
given, unedited.

### 3. Set the variables

On the app service → Variables. `.env.example` documents all of them; these are
the ones with no usable default:

| Variable | Value | Why |
| --- | --- | --- |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` | See above. **Never SQLite here.** |
| `TZ` | `America/Detroit` | A container defaults to UTC, which moves every day boundary 4–5 hours and files offers under the wrong day |
| `TOWBOOK_USER` | the portal login | Single company. Per company it is `TOWBOOK_<PREFIX>_USER` — see Multiple companies |
| `TOWBOOK_PASS` | the portal password | |
| `DASHBOARD_PASSWORD` | `1234` | The board's shared password. **Change it before sharing the URL** — see the warning below |
| `SESSION_SECRET` | 64 random hex chars | Unset means a new secret per process, so every redeploy logs everyone out. `python -c "import secrets; print(secrets.token_hex(32))"` |
| `RUN_SCHEDULER` | `true` | Default. Set `false` only when the scheduler has its own service |
| `ANTHROPIC_API_KEY` | optional | Without it the Analyst is skipped and the run is `partial`, not failed |

Do **not** set `PORT` — Railway assigns it and the app reads it.

> **`1234` is not a real password.** It is what was asked for and it is what
> ships. It is adequate for a URL nobody has been given; it is *not* adequate
> for real customer data, and it is emphatically not adequate once several
> towing companies' numbers sit behind one login. Change `DASHBOARD_PASSWORD`
> before the link leaves your hands. The login page says the same thing while
> the default is still in place.

### 4. Deploy

Railway builds on push. The first boot does more than later ones:

1. **`alembic upgrade head`** — runs in-process before anything serves. Safe on
   a brand new empty database, on one that is already current, and on one an
   older `initdb` created without a version row. It never crashes the container:
   a failed migration comes up and shows a banner, because a crash-loop tells
   you nothing when there is no SMS to fall back on.
2. **Cold-start backfill** — with an empty `requests` table it pulls the
   trailing 30 days once, so the first deploy shows real numbers instead of
   empty tabs. One acquisition for the whole window; each day's metrics are then
   computed from the stored rows with no further network calls. Runs once, ever.
   `BOOTSTRAP_ON_EMPTY=false` disables it, `BOOTSTRAP_DAYS` changes the window.
3. **The scheduler starts** inside the web process and the board begins serving.

The health check is `GET /healthz` (exempt from the password gate). It reports
the database, the migration revision and whether the scheduler is running:

```json
{"ok": true, "database": {"backend": "postgresql", "tables": 10},
 "migration": {"ok": true, "revision": "0004"},
 "scheduler": {"running": true, "jobs": 4}}
```

### 5. First login

Open the Railway-provided domain (Settings → Networking → Generate Domain).
You will get the login page; enter `DASHBOARD_PASSWORD`. The session cookie
lasts `DASHBOARD_SESSION_DAYS` (30 by default).

If the board looks empty, check `/health` — it distinguishes "no data has been
collected yet" from "collection is failing".

### Why the scheduler runs in the web process

One service, one process, one worker. `serve` starts APScheduler on a background
thread from the FastAPI lifespan hook, so **if the board is up, the data behind
it is being refreshed**. The alternative — a web service and a separate worker —
has a failure mode this design does not: the worker dies, the board stays up,
and it goes quietly stale. There is no text message that would have told you.

Two guards stop a duplicate scheduler, in this order:

1. **One uvicorn worker.** No `--workers` flag appears in `Procfile`,
   `railway.json` or `nixpacks.toml`, and `numReplicas` is 1. Do not add one:
   N workers is N schedulers, each pulling the Towbook API on the same cron.
2. **A PostgreSQL advisory lock** (`core/leader.py`). The first guard is a
   promise about how the process is launched, and that promise survives until
   somebody scales to two replicas from the Railway UI — one click, no error.
   The second process asks for the lock, is refused, and comes up as a
   read-only board. Nothing breaks either way: every job is idempotent, so a
   duplicate run recomputes a window rather than doubling it.

**To split the scheduler out later** (no code change):

1. set `RUN_SCHEDULER=false` on the web service;
2. add a second Railway service from the same repo with the start command
   `python -m towbook_agent schedule`.

### Notifications are off, and the banner is why that is safe

`config/notifications.yaml` ships with **every route carrying
`enabled: false`** — no SMS, no email, nothing sent anywhere. The routing table,
recipients, templates and quiet hours are all still there and correct; turning a
channel on is `enabled: true` on one line plus that channel's credentials. No
code change, no redeploy.

That is a fine trade for a *report* — a report is something you go and look at.
It is not a fine trade for a *failure*, so failures are delivered by the board
instead: `web/queries.pipeline_banner()` puts a persistent red banner across
**every tab** when the last scheduled run failed, when a report is overdue
(the watchdog's own logic, asked read-only), or when a `pipeline_failure` was
recorded in the last 24 hours. There is no dismiss button and it does not render
when the pipeline is healthy.

If you want a text as well, re-enable the `pipeline_failure` route first — it is
the only thing here that cannot wait for somebody to open the board.

### Playwright is not installed, and does not need to be

The default acquisition path is `source: api`: plain HTTP to Towbook's JSON
endpoint via httpx. Every `playwright` import in `agents/acquisition.py` is lazy
or behind `TYPE_CHECKING`, so **nothing on the default path imports it** and the
container carries neither the wheel nor a browser. That is several hundred MB
and a browser download saved on every build.

`source: ui` (the Playwright fallback in `config/schedule.yaml`) needs both:

```toml
# nixpacks.toml
[phases.install]
cmds = [
  "python -m pip install --upgrade pip",
  "python -m pip install --no-cache-dir -r requirements.txt 'playwright>=1.44'",
  "python -m playwright install --with-deps chromium",
]
```

Locally: `pip install ".[ui]" && python -m playwright install chromium`.

### Deploying somewhere else

`Procfile` carries the same process definitions in a provider-neutral format, so
Render, Fly, Dokku and Heroku work unchanged. Everything above applies verbatim
except the name of the dashboard you click in — including the ephemeral
filesystem, which is true of all of them.

The reasoning behind each deployment setting is in `RAILWAY_NOTES.md`.

---

## Layout

```
towbook_agent/
  agents/    acquisition, ingestion, classifier, metrics, analyst, notifier
  core/      paths, config_loader, safe_eval, logging_setup, models, db, events, scheduler
  web/       app, queries, rules_admin, templates/, static/
config/      the YAML files, including companies.yaml (the tenant roster)
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
