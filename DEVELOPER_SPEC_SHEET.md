# Towbook Job Acceptance Intelligence System

### Technical & Capability Specification

**Version 0.1.0** · Python 3.12 · FastAPI · PostgreSQL
**Audience:** towing company owners · motor club / digital dispatch providers · engineering evaluators

---

> **Before circulating this document:** the *Field Evidence* section (§3) contains real
> operating figures from a live deployment — acceptance rates, decline volumes and
> estimated revenue. Redact that section before sending this to a competitor, a client
> or a prospect.

---

## 1. What this is

A reporting and intelligence system that sits on top of **Towbook Digital Dispatch** and
answers one question the portal does not:

> **What work are we being offered that we are not getting — and what is it costing us?**

Towbook tells you what you accepted. It does not total what you turned away, attribute it
to a cause, put a dollar figure on it, or tell you which hour of which day it keeps
happening in. This system does all four, on a schedule, without a human opening a browser.

It is **read-only against Towbook**. It never accepts, declines, edits or dispatches
anything. It observes, measures and reports.

### The core insight

Acceptance rate is a *symptom*. The useful deliverable is an **inventory of missed work,
attributed to a cause, with an action attached** — which turns "we're missing work" into
"nobody is covering 18:00–22:00, and that specific gap cost $18,000 last month."

---

## 2. What it measures

### 2.1 Every offer lands in exactly one bucket

Derived from the Towbook status code. Buckets are defined in configuration, not code, so
they can be re-cut without a deployment.

| Bucket | Meaning | Ours to fix? |
|---|---|---|
| `won` | Accepted | — |
| `in_flight` | Still deciding | — |
| `declined` | **We said no**, with a reason | **Yes** |
| `no_response` | **Nobody answered in time** | **Yes** |
| `accept_failed` | We tried to accept and it failed | **Yes — technical** |
| `client_withdrew` | Client pulled it | Reported separately |

`client_withdrew` is deliberately **excluded** from the headline recoverable figure.
Some withdrawals are the client's doing; others are a client giving up because we were
slow — and the feed cannot distinguish them. Both numbers are always shown, so the
judgement is visible rather than baked in.

### 2.2 Root cause and remedy

Declined offers carry a denial reason, mapped to a cause class and a remedy class:

| Cause | Remedy class | The question it answers |
|---|---|---|
| `equipment` | capital | *Which truck class is missing?* |
| `staffing` | scheduling | *Which hours are uncovered?* |
| `coverage` | territory | *Where, and how far out?* |
| `information` | process | *Cheap fix — ask before declining* |
| `review` | review | *Reason not captured — needs a human* |
| `unrecorded` | data | *We declined without recording why* |

`no_response` has no reason field by definition. Its cause is always **attention**, and
its remedy is **coverage during a specific window** — which is what the hour-level
analysis quantifies.

### 2.3 The dollar model — and its limits

**Towbook's `offerAmount` field is empty on 100% of records.** There is no dollar figure
anywhere in the feed. Every financial number this system produces is therefore an
**estimate**, built as:

```
recoverable missed jobs  ×  owner-supplied gross average job value per client
    +  declined light-service jobs  ×  flat light-service rate
```

Four rules make that estimate defensible rather than decorative:

1. **Gross, not margin.** Stated on every page that shows a dollar.
2. **No invented defaults.** A job whose client has no configured value contributes
   **nothing** — and is counted in `unpriced_jobs`, printed beside the total, with the
   responsible clients named. The number understates *visibly*.
3. **Tow averages are never applied to light service.** Pricing a missed tire change at a
   tow's worth would inflate the single number most likely to be quoted out loud. Light
   service is valued at its own flat rate and reported as a separate line.
4. **Part-periods are flagged.** A 30-day window makes "July" mean Jul 4–31, so the row
   is labelled *part month, 28 of 31 days*. A clipped total is never presented as a
   period total.

---

## 3. Field evidence

*Live two-entity deployment, 30-day window. Real figures — redact before circulating.*

### 3.1 The headline

| Measure | Entity A | Entity B | Combined |
|---|---:|---:|---:|
| Offers received | 2,564 | 182 | 2,746 |
| Acceptance rate | 39.6% | 46.7% | 40.1% |
| Miss rate | 60.0% | 52.7% | 59.5% |
| Recoverable missed jobs | 975 | 94 | 1,069 |
| **Estimated loss (30 days)** | **$56,595** | **$7,990** | **$64,585** |

At that run-rate the combined figure straight-lines to roughly **$785,000 per year** of
work offered and not captured.

### 3.2 The finding that pays for the system

Staffed window: **Mon–Fri 06:00–18:00**.

| | No-response rate |
|---|---:|
| Offers **inside** the staffed window | **3.9%** |
| Offers **outside** the staffed window | **47.1%** |

**A twelve-fold difference.** This is not a truck problem or a pricing problem — it is a
coverage problem, and it is the single most actionable number the system produces.

### 3.3 The most expensive hour

Ranked by **money**, not job count:

| Hour | Lost (30d) | Jobs | Unstaffed share |
|---|---:|---:|---:|
| **18:00–18:59** | **$5,895** | 93 | 100% |
| 17:00–17:59 | $5,355 | 85 | 37% |
| 19:00–19:59 | $4,685 | 75 | 100% |
| 20:00–20:59 | $3,850 | 62 | 100% |

**The most expensive hour of the day is the hour the shift ends** — and it is the worst
hour for *both* entities independently. The 18:00–22:00 block alone accounts for
approximately **$18,000 per month**.

A second, quieter finding: at midday the weekday desk misses **8** jobs in 30 days; the
weekend at the same hour misses **32**. Midday is not a staffing problem Monday to
Friday — the weekend is a hole.

### 3.4 Why hours must be ranked by dollars

The system carries two hour-level views and they deliberately disagree:

- **Blind spots** ranks hours by *how often* offers go unanswered.
- **Lost revenue** ranks hours by *what going unanswered cost*.

They diverge whenever a quiet hour is full of tows and a busy one is full of tire
changes. Staffing decisions should be made on the second ranking.

---

## 4. For towing company owners

| You get | What it replaces |
|---|---|
| A running lost-revenue figure — day, week, month to date | Guesswork, or a spreadsheet nobody updates |
| The exact hours costing you the most, in dollars | "We're busy at night, I think" |
| Missed work attributed to cause, with a remedy class | An acceptance rate with no next step |
| A close-off list: work you *don't* want, by client | Declining the same job type 200 times |
| Per-client comparison — who sends good work | A gut feeling about a motor club |
| Print-ready PDF reports on your letterhead | Nothing |
| Hourly / daily / weekly / monthly SMS + email | Logging in to check |

**The argument it builds for you:** not "we should staff evenings," but "18:00 to 22:00
costs us $18,000 a month, our no-response rate outside staffed hours is 47% against 3.9%
inside them, and one dispatcher covering that block pays for itself in the first week."

That is a staffing case with evidence attached, and it survives contact with a banker.

---

## 5. For motor club / digital dispatch providers

The same dataset answers the provider-side question — *why is our work not being
covered?* — from the operator's side of the glass, which is normally invisible to you.

| Signal | What it tells a provider |
|---|---|
| **No-response rate by hour of week** | Where offers are landing when nobody is watching, per operator |
| **Median response window** | Measured at **3 minutes** on this account (mean 4, max 15) — how long an operator actually has |
| **Decline reason mix, normalised** | Equipment vs staffing vs territory vs *"not enough information"* |
| **`information` cause class** | Offers declined purely for missing detail — a **provider-side fix**, not a capacity problem |
| **Territory banding by ZIP** | Whether offers are being sent outside a provider's real service area |
| **Service-type acceptance split** | Which work an operator wants and which they never take |
| **Close-off candidates** | Work an operator will *never* accept — stop sending it and both sides win |

**The commercial case for a provider:** offers that expire unanswered are pure waste on
both sides of the transaction. This system identifies, per operator, exactly which hours
and which service types are structurally uncoverable — allowing routing rules to be
tuned before the offer is broadcast rather than after it expires.

The `information` cause class is the cheapest win in the dataset: those are jobs an
operator *wanted* and declined only because the offer did not say enough.

---

## 6. Application surface

**14 views**, each scoped to one company or to a merged multi-entity view.

| View | Route | Purpose |
|---|---|---|
| Hourly | `/hourly` | Current-day running acceptance, auto-refreshing |
| Weekly | `/weekly` | Week performance and trend |
| Monthly | `/monthly` | Month performance and trend |
| Trends | `/trends` | Long-run direction |
| Missed work | `/` | The recoverable inventory — the primary view |
| **Lost revenue** | `/revenue` | Running dollar total; hours ranked by cost |
| Maps | `/maps` | Offered heat map + declined-jobs map by ZIP centroid |
| Blind spots | `/blind-spots` | 7 × 24 hour-of-week grid of unanswered offers |
| Close-off | `/close-off` | Work to stop being offered, grouped by client |
| Live | `/live` | Today, as it happens |
| Daily | `/daily` | Single-day detail |
| Clients | `/clients` | Per-client comparison and drill-down |
| Rules | `/rules` | The active configuration, rendered readable |
| Health | `/health` | Pipeline state, data quality, last run |

**39 HTTP routes** total, including a JSON API (`/api/*`) and an unauthenticated
liveness probe (`/healthz`) that leaks nothing.

Every screen has a **Print** button producing a letterhead PDF via the browser's own
print dialog — no PDF library, no server-side rendering.

---

## 7. Architecture

### 7.1 Pipeline

```
  Towbook JSON API
        │   authenticate → switch company → page results
        ▼
  acquisition_api ──► raw/YYYY/MM/DD/run_<ts>.json      (verbatim archive)
        │
        ▼
  ingestion ──► classifier ──► duplicates ──► PostgreSQL
        │
        ▼
  metrics ──► missed_work ──► analyst (optional LLM) ──► notifier
        │
        ▼
  FastAPI dashboard  +  SMS / email delivery
```

### 7.2 Modules

**Agents** (`towbook_agent/agents/`) — `acquisition_api`, `acquisition` (Playwright
fallback), `ingestion`, `classifier`, `duplicates`, `metrics`, `missed_work`, `analyst`,
`notifier`

**Core** (`towbook_agent/core/`) — `companies`, `config_loader`, `db`, `events`,
`leader`, `logging_setup`, `models`, `paths`, `safe_eval`, `scheduler`

**Scale:** ~32,900 lines of application code, ~16,100 lines of tests.

### 7.3 Acquisition — verified against the live portal

The system reads Towbook's internal JSON endpoint
(`/api/digitaldispatch/callrequests`) over an authenticated cookie session. **No browser
is required.** A Playwright path exists as a fallback but is not the default — the JSON
records carry a real unique id (`callRequestId`), whereas the Excel export's only
id-shaped column is blank on precisely the unaccepted offers being measured.

**One login, several companies.** The endpoint has **no `companyId` parameter** — it
returns whatever company the *session* is currently switched to, and a fresh login lands
on the user's home company. An account carrying several towing entities therefore reports
only the home one unless the session is switched first.

The system performs the portal's own switch (`GET /change?c=<id>`, the book icon in the
top-right) and **confirms it twice**: once from the `data-current-company-id` attribute
every authenticated page carries, and again against the `companyId` on the returned rows.
Both checks are **fatal on mismatch and deliberately not retried** — rows filed under the
wrong company are permanent and invisible.

### 7.4 Data model

Nine data tables, plus the Alembic migration ledger. **Every data table carries
`company_id`.**

| Table | Holds |
|---|---|
| `requests` | One row per offer, de-duplicated |
| `runs` | Pipeline execution record |
| `metrics_hourly` | Hourly counters + running day totals |
| `metrics_daily` | Daily metric documents |
| `metrics_weekly` | Weekly metric documents |
| `metrics_monthly` | Monthly metric documents |
| `metrics_missed_work` | Missed-work documents per period |
| `client_daily` | Per-client daily aggregates |
| `alerts_fired` | Alert de-duplication ledger |

**Duplicate collapse:** one job a club broadcasts three times is **one row**, carrying
`duplicate_count` and the references of the offers it stands for. Both the dashboard and
the emailed report read through the same collapse function, so they cannot disagree about
how many jobs a day held.

### 7.5 Multi-entity support

- Roster defined in `config/companies.yaml`; credentials **never** in that file — only
  the *name* of the environment-variable prefix holding them.
- Every query is wrapped by a `for_company` decorator that resolves and **activates** the
  company for the duration of the call. There is no code path that reads "all companies":
  `company_id=None` means *the active one*, never *everything*.
- Activation also resolves timezone — a Texas entity's "today" is not an Ohio entity's.
- A **merged scope** (`__all__`) reads several entities as one book. It is a *way of
  reading*, not a company: no login, no Towbook id, no stored rows, no pipeline. It is
  recomputed on every page load.
- Where members disagree on something a number depends on — a staffed window, a timezone,
  a client's job value — the merged view uses the default company's setting and **prints
  a sentence saying so**. It never silently blends two staffed windows into a coverage
  figure nobody could defend.

---

## 8. Configuration

Behaviour lives in YAML, not code. Files hot-reload; the loader keys its cache on file
mtime and size.

| File | Governs |
|---|---|
| `companies.yaml` | The entity roster, staffed windows, job values, letterheads |
| `rules.yaml` | Buckets, causes, remedies, thresholds, territory bands, pricing |
| `schedule.yaml` | Cron jobs and their date ranges |
| `notifications.yaml` | Channels, recipients, templates |
| `selectors.yaml` | Portal endpoints and DOM selectors |
| `schema.yaml` | Source column mapping |

**Override precedence** (later wins): `rules.yaml` → per-company `coverage:` →
per-company `job_value_by_client:` → per-company `rules:` (deep-merged).

Coverage windows and price lists **replace** rather than merge — a half-inherited staffed
window or a half-inherited price list is a number nobody can defend.

**Deleting `companies.yaml` is safe.** With no roster the system runs as a single company
whose id is `default`.

### Scheduled jobs (default)

| Job | Cron | Window |
|---|---|---|
| hourly | `0 * * * *` | Previous full hour |
| daily | `0 6 * * *` | Previous calendar day |
| weekly | `0 6 * * 1` | Trailing 7 days |
| monthly | `0 6 1 * *` | Previous calendar month |

---

## 9. Operations

**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.0 · Alembic · APScheduler · Jinja2 ·
httpx · PostgreSQL (SQLite supported for local development)

**Delivery:** Twilio SMS · SMTP email · web dashboard

**Optional:** Anthropic Claude for narrative analysis. The analyst is strictly optional —
the system produces every number without it, and the LLM is never permitted to originate
a figure that is not already in the computed metrics.

**Deployment:** Railway (Nixpacks). Also runs under any WSGI/ASGI host or bare
`uvicorn`.

**Resilience:**
- Leader election so multi-replica deploys run the scheduler exactly once; the lock
  reaps orphaned holders rather than deadlocking.
- Credentials come from the environment only, and a logging filter scrubs them from
  every log line.
- Raw payloads archived verbatim before parsing, so any metric can be re-derived from
  source without re-contacting the portal.
- A pipeline failure raises a banner that retires itself once a later run succeeds.

**Quality:** **844 automated tests**, all passing. The suite runs with no network access
(the socket layer is patched so an accidental outbound call fails loudly), against a
throwaway repo sandbox and a fresh database per test.

---

## 10. Security posture — stated plainly

This section is deliberately blunt. Read it before deploying anything to a public URL.

**What is in place**

- Signed session cookie (HMAC, standard library — no added dependency).
- The signature covers a fingerprint of the password, so rotating the password
  invalidates every live session immediately.
- Timing-safe comparison on both password and signature.
- Open-redirect protection on the post-login and company-switch redirects.
- Credentials read from environment variables only; scrubbed from logs.
- `/healthz` exempt from auth and leaks nothing.

**What is *not* in place**

- **A single shared password guards the entire dashboard.** The shipped default is
  `1234`, and the login page says so in a banner until it is changed. Set
  `DASHBOARD_PASSWORD` to something long and random before the board holds real data.
- **No per-user accounts, no audit trail, no lockout after repeated guesses.** Anybody
  who learns the password has everything.
- **The company switcher performs no identity check.** Any authenticated session can
  select any configured company. This is correct for one owner with several entities —
  the current design target — and is **not** sufficient to host competing customers on
  one instance.

**Status: multi-*company*, not yet multi-*tenant*.**

The data plane is genuinely isolated — every table carries `company_id`, every query is
scoped, and there is no "read everything" path. That is the expensive half and it is
done. What remains before this could be sold as hosted software to unrelated customers:

1. User accounts with a tenant membership table
2. Membership enforced at the switcher *and* at the query decorator
3. Self-service provisioning (today: edit YAML, set env vars, redeploy)
4. Audit logging, billing, per-tenant rate limiting

The data model does not need to change to support any of it.

---

## 11. Roadmap

| Item | Status |
|---|---|
| Multi-entity roster, merged view | **Shipped** |
| Company switching on a shared login | **Shipped** |
| Lost-revenue view: running totals + hours ranked by cost | **Shipped** |
| Maps: offered heat map, declined-jobs map | **Shipped** |
| Print-ready letterhead PDF | **Shipped** |
| Dollar figures in scheduled SMS / email reports | Not yet — dashboard only |
| Tenant identity & access control | Deferred by design |
| Self-service provisioning, billing | Not started |

---

## 12. Verified portal facts

Behaviour confirmed against the live Towbook portal, not inferred from documentation:

- `offerAmount` is **empty on 100% of records**. No dollar figure is derivable from the
  feed; every financial number originates from owner-supplied averages.
- The `callrequests` endpoint has **no `companyId` parameter** — results are scoped to the
  session's active company.
- `endDate` is **inclusive** of that calendar day; there is no time-of-day filter, so an
  hourly job pulls the calendar day and metrics trims the hour.
- Every authenticated page carries `<body data-current-company-id="…">`, which is what
  makes a company switch *verifiable* rather than merely requested.
- The company switcher's menu items are plain links (`/change?c=<id>`) and work on a
  cookie session with no browser.
- Towbook publishes **no response timestamp**. The response window is measured
  observationally: median 3 minutes, mean 4, max 15.
- `pickup_zip` is populated on effectively all records, which is what makes ZIP-centroid
  mapping viable without a geocoding service.

---

*Specification generated from the live codebase at version 0.1.0. Every figure in §3 and
§12 was measured against the running system, not estimated.*
