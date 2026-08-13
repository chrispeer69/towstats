# Multi-tenant build plan

Getting from 2 companies to 20 customers, and what happens when Towbook
notices.

---

## The thing that reframes this plan

towSTATS currently reads **Towbook** — a competitor's product — on behalf of
customers, in order to show them a five-figure monthly missed-revenue figure.
The natural next question a customer asks is "what would fix this?", and the
answer is **US Tow Dispatch**, which you own.

That is an excellent commercial position and a poor engineering assumption. It
means:

1. **Towbook is not a neutral upstream. It is an adversary with a shutoff
   switch.** Every mitigation below has to assume they eventually use it.
2. **The US Tow Dispatch adapter is not a phase-4 nice-to-have.** It is the
   only thing that converts a Towbook cutoff from an outage into a sales event.
3. **A customer already on US Tow Dispatch is a strictly better customer** —
   no credentials to hold, no scraping, no rate limits, no ToS exposure, and
   richer data. Sign those first.

**One correction to earlier advice.** I previously suggested approaching
Towbook for a sanctioned API or partnership. Given that you own a competing
dispatch platform and that towSTATS makes the case for switching to it, that is
now bad advice — it announces the pattern to the one party with both the motive
and the ability to end it, at the moment you are least able to absorb it.
Withdraw that.

---

## Where the system is today

**Already done and tested.** Row-level tenant isolation (`company_id` on every
table, every query filtered), per-company timezone / coverage / job values /
letterhead, the merged read scope, per-company accounts with a registry-level
visibility fence, and per-company failure isolation in the scheduler — one
tenant's expired password cannot stop the rest of the roster
(`core/scheduler.py -> _run_job`, one try block per company).

**The honest gap list for 10–20 customers:**

| # | Problem | Consequence at 20 customers |
|---|---|---|
| 1 | Roster lives in `config/companies.yaml`, committed to git | Every new customer, price change, or staffed-hours tweak is a commit + redeploy |
| 2 | Credentials are per-company env vars | 40 Railway variables; every rotation is a redeploy; no audit trail |
| 3 | `SESSION_SECRET` unset | Every redeploy signs out every user at every customer |
| 4 | Serial acquisition loop, all tenants at `:00` | 20 logins in a burst, 24×/day, from one IP — a detectable signature |
| 5 | Health is per-install, not per-tenant | No way to answer "which of my 20 is broken right now" |
| 6 | No alerting to *you* | The failure banner renders on the customer's board. They find out first |
| 7 | No entitlement concept | Suspending a non-payer means editing YAML and redeploying |
| 8 | Single source (`api`), single fallback (`ui`), both Towbook | One upstream decision ends all 20 simultaneously |

Numbers 1 and 2 are what actually block growth. Number 8 is what ends the
business. Everything else is friction.

---

## Phase 0 — finish what's on the bench

Small, all of it already understood. Do it before customer #3.

- [ ] **Ship the accounts work.** It is written and tested (38 tests) but
      uncommitted. Nothing else in this plan is safe to sell without it
- [ ] **Set `SESSION_SECRET` on Railway.** One variable. Today every redeploy
      logs everyone out; at 20 customers that is 20 support calls per deploy
- [ ] **Set a real `DASHBOARD_PASSWORD`,** then create the operator account and
      let it die
- [ ] **Turn off the hourly Analyst.** 96% of API spend, on commentary about a
      single hour that nobody reads. One line in `config/schedule.yaml`
- [ ] **Measure real token cost** on one live tenant with `count_tokens`, so
      pricing is built on a number instead of my estimate
- [ ] **Fix the two failing tests** (`test_revenue.py`,
      `test_web.py::test_every_tab_states_what_unit_its_numbers_are_in`). They
      predate this work, and a red suite hides the next real regression

---

## Phase 1 — get tenants out of git

**This is the growth blocker.** Target: onboarding a customer is a form
submission, not a deploy.

- [ ] **Move the roster to a database table.** Mirror the `companies.yaml`
      schema exactly. Keep the YAML loader as seed-and-fallback — the existing
      `core/companies.py` already degrades gracefully when the file is absent,
      so the DB becomes one more source in front of it
- [ ] **Encrypt credentials at rest** in that table, with one master key in the
      environment. Forty env vars collapse to one, and rotation stops being a
      deploy
- [ ] **Company admin screen** (operator-only, next to `/accounts`): add, edit,
      enable/disable, and a **Test credentials** button that attempts a login
      and reports back before the customer is ever told they are live
- [ ] **`enabled` becomes your entitlement switch.** Non-payer flips to off in
      the UI; the scheduler stops running them and their board goes dark, with
      no code change and no data loss
- [ ] **Hot-reload the roster** the way the YAML already does, so adding a
      company does not need a restart

Result: customer #11 through #20 cost you 30 minutes each and zero deploys.

---

## Phase 2 — the US Tow Dispatch adapter

**The strategic item.** Runs in parallel with Phase 1 if you have the capacity;
it is not gated behind it.

The bone structure already exists: `config/schedule.yaml` has
`source: api | ui`, both acquirers archive to `raw/`, and ingestion dispatches
on the archived file's own format. That is an adapter pattern that was never
named. Name it, then add the third implementation.

- [ ] **Discovery first.** The site advertises open data export and third-party
      integration. Find out what that actually is — REST, webhook, S3 drop,
      scheduled CSV — before designing anything. This is a half-day, and it
      determines the rest
- [ ] **Formalize the source interface.** One protocol: authenticate, pull a
      window, archive raw, hand off to ingestion. Three implementations:
      `towbook_api`, `towbook_ui`, `ustd`
- [ ] **Per-company `source:`** in the roster, so a single install serves
      Towbook tenants and US Tow Dispatch tenants side by side
- [ ] **Dual-source parity mode.** During a migration, pull from both for a
      period and diff the offer counts. This is what lets you tell a customer
      "your numbers will not change when you switch" and be believed
- [ ] **History survives the switch.** Same `company_id`, new source. The
      `id` field never changes, so a migrated customer keeps every stored
      offer, every metric, and every month of trend

Once this ships, a Towbook cutoff stops being an outage and becomes a
conversation you were going to have anyway.

---

## Phase 3 — operating 20 tenants

- [ ] **Fleet health board** (operator-only): every tenant, last successful
      run, staleness, credential status, last error. One screen answers "what
      is broken"
- [ ] **Alert the operator, not just the board.** `notifications.yaml` already
      has the routing table intact with every route disabled — turn on a single
      email route for `pipeline_failure`, addressed to you. You cannot watch 20
      dashboards
- [ ] **Stagger acquisition.** Spread tenants across the hour instead of all
      firing at `:00`. This is a Towbook-detection mitigation as much as a load
      one — 20 simultaneous logins from one IP is a pattern; 20 spread over 50
      minutes is traffic
- [ ] **Raise the connection pool.** `DB_POOL_SIZE=5` / `DB_MAX_OVERFLOW=5` was
      sized for one owner. Web traffic from 20 tenants plus the scheduler will
      find that ceiling
- [ ] **Run a restore drill.** Not "is there a backup" — actually restore
      Postgres into a scratch service and confirm the board renders. Untested
      backups are decoration
- [ ] **Per-tenant usage visibility,** so you know which customer is expensive
      before the invoice tells you

---

## Towbook: threat model and response

Ordered by likelihood. **Both of the top two get more likely as you scale** —
at two customers you are invisible; at twenty you are a pattern in someone's
log.

### 1. Silent schema change — most likely, worst failure mode

A field renames, a status value changes, the response shape shifts. The pull
succeeds, ingestion succeeds, and the numbers are quietly wrong. A customer
makes a staffing decision on a bad figure and you do not find out for weeks.

**Response — build this in Phase 1, it is cheap:** a per-pull canary that
asserts invariants before ingestion commits.

- Row count within a sane band of the same weekday's recent average
- `callRequestId` present and unique (verified unique across 3,079 rows today)
- Every required field non-null at the expected rate
- No sudden 100%-of-one-status distribution

Fail the run loudly rather than ingesting. A missing report is recoverable; a
confidently wrong one is not.

### 2. Rate limiting or IP blocking

Twenty logins per hour from one Railway IP, 480 a day, forever.

**Response:** stagger (Phase 3). Cache sessions rather than re-authenticating
every pull. Consider whether hourly acquisition is even needed per tenant, or
whether most customers are well served by daily.

### 3. Auth hardening — MFA, CAPTCHA, device checks

Kills the `api` and `ui` paths together. No engineering answer.

**Response:** Phase 2 is the answer. There is no other one.

### 4. Terms enforcement or deliberate cutoff

You are automating access to a competitor's product to sell a service that
argues for leaving it. Customer authorization (now in `ONBOARDING.md`) is worth
having and does not bind Towbook's terms.

**Response:**
- Have a lawyer read Towbook's current ToS against what this system does,
  before customer #10, not after
- Never market towSTATS as reading Towbook. Market the outcome
- Keep the Phase 2 migration path warm enough that a cutoff is a Tuesday

### The posture

Treat Towbook as a **supported but untrusted upstream on a countdown**. Every
Towbook-sourced customer is revenue with an expiry date attached; every US Tow
Dispatch customer is revenue you control end to end. Price and prioritize
accordingly.

---

## What I need decided before building

1. **Sequencing.** Phase 1 (growth) and Phase 2 (survival) both want to be
   first. If the next ten customers are already on US Tow Dispatch, Phase 2
   leads and Phase 1 can wait. If they are Towbook accounts, Phase 1 leads. Who
   are the next ten?
2. **US Tow Dispatch data access.** What does the export actually expose, and
   can I get credentials for a test account?
3. **Hosting shape.** This plan assumes one shared install. Still the right
   call — separate services multiply hosting and deploys without reducing API
   spend, and the isolation is now enforced in code and tested. Only revisit if
   a customer contractually demands separate infrastructure.
4. **Acquisition cadence.** Does every tenant need hourly, or is daily enough
   for most? This changes both API cost and Towbook exposure.
