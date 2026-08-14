# The demo tenant

A fully seeded, fully working board a prospect can log into. It runs the real
application against a **separate roster and a separate database**, so nothing
about it can touch a paying customer's numbers.

---

## The login

| | |
|---|---|
| Username | `demo` |
| Password | `summit-demo-2026` |
| Company | Summit Towing & Recovery (`example-towing`) |
| Role | `member` — **not** operator |

The account is scoped to the demo tenant and nothing else. That scope is
enforced by middleware around every request (`web/auth.py`
→ `PasswordGateMiddleware`), not by a check on each endpoint, so it holds for
endpoints written later by someone who never read that file. Verified: the
switcher lists only Summit, `merged_available` is false, and
`/company/default`, `/company/auto-lyft` and `/company/__all__` all redirect
back to `/` rather than answering.

Publishing this password on the marketing site is safe by design — it opens one
synthetic company containing no real data.

---

## Running it

```bash
cd towbook-agent
ROOT="$(pwd -W)"                  # NOTE: pwd -W, not pwd. See "The path trap".

TOWBOOK_REPO_ROOT="$ROOT/demo-root" \
DATABASE_URL="sqlite:///$ROOT/demo-root/data/demo.db" \
RUN_SCHEDULER=false \
python -m towbook_agent serve --host 127.0.0.1 --port 8899 --no-scheduler
```

Then <http://127.0.0.1:8899>.

**The scheduler must stay off.** The demo tenant has no Towbook account and
never will; with the scheduler on, the install would try to acquire for a
company with no credentials, every hour, forever.

## Rebuilding the data

```bash
ROOT="$(pwd -W)"
rm -f demo-root/data/demo.db* demo-root/raw/*.json

TOWBOOK_REPO_ROOT="$ROOT/demo-root" \
DATABASE_URL="sqlite:///$ROOT/demo-root/data/demo.db" \
python scripts/seed_demo.py --weeks 13
```

Takes about a minute. It regenerates the offers, ingests them through the real
ingester, computes daily/weekly/monthly/hourly metrics through the real metrics
passes, and recreates the demo login. Re-run it whenever the board should read
as current — the window always ends at *now*.

---

## How it is isolated

`core/paths.py` reads `TOWBOOK_REPO_ROOT` and resolves `config/`, `data/`,
`state/` and `raw/` beneath it, while the **code** still loads from the
installed package. So the demo is the same application, not a fork:

```
demo-root/
  config/     its own roster: one company, enabled, no credentials
  data/       its own SQLite database (gitignored)
  raw/        its own archives (gitignored)
  state/      its own logs (gitignored)
```

Three consequences worth knowing:

* **`demo-root/` has no `.env`,** so the demo process never loads the
  production Towbook, Anthropic, Twilio or SMTP credentials at all.
* **`dashboard_users` lives in the demo database.** Creating the demo account
  switched *that* database into accounts mode. Production is untouched and
  still uses the shared `DASHBOARD_PASSWORD`.
* **`source_timezone` is `America/Chicago`** in the demo schema, because the
  tenant trades in Fort Worth and that setting is install-wide.

---

## What the demo shows

Over the trailing 30 days, from the seeded data:

| | |
|---|---|
| Offers | 2,708 |
| Accepted | 1,613 (59.6%) |
| Missed | 1,095, of which 1,000 recoverable |
| **Estimated gross missed** | **$35,305** |
| Unanswered **outside** staffed hours | **47.8%** |
| Unanswered **inside** staffed hours | **4.7%** |
| Blind-spot windows | 83 hour-of-week cells |
| Close-off candidates | Lockout (8.3% accepted), Fuel Delivery (2.8%) |

The two coverage figures are anchored to the real measured contrast from the
live deployment — 5.5% inside against 48.7% outside — which is the same pair
quoted on the marketing site. The demo therefore argues the same case as the
site, with the same numbers.

Every offer carries a ZIP and a distance, and 100% of them geocode, so the maps
views are populated rather than reporting everything as `unmapped`.

---

## The path trap

`DATABASE_URL` with a relative path is resolved against `TOWBOOK_REPO_ROOT`.
Git Bash expands `$(pwd)` to `/c/Users/...`, which **Windows does not consider
absolute**, so

```bash
DATABASE_URL="sqlite:///$(pwd)/demo-root/data/demo.db"   # WRONG
```

quietly becomes `<root>/c/Users/.../demo.db`. The seeder fills a real database
in a stray directory tree, reports complete success, and the board reads a
different, empty file. Every step looks fine.

`scripts/seed_demo.py` now refuses to run when the resolved path lands outside
the demo root, and names `pwd -W` in the error. Use `pwd -W`.

---

## Putting it on the web

The demo needs **its own Railway service**, from this same repo. The isolation
is entirely environment variables, and those are per-service.

| Setting | Value |
|---|---|
| Start command | `python start_demo.py` |
| `TOWBOOK_REPO_ROOT` | `/app/demo-root` |
| `DATABASE_URL` | `sqlite:////app/demo-root/data/demo.db` (four slashes — absolute) |

Set no Towbook, Anthropic, Twilio or SMTP variables. The demo tenant has no
account and contacts nothing; `start_demo.py` forces `RUN_SCHEDULER` and
`BOOTSTRAP_ON_EMPTY` off so nothing can try.

It does **not** need its own domain — Railway issues a `*.up.railway.app` URL.
For sales, add `demo.ustowstats.com` as a custom domain on the service and
point a CNAME at the target Railway gives you, the same way `www` is set up for
the marketing site.

### The ephemeral filesystem is the refresh mechanism

A container's disk does not survive a redeploy, so a SQLite database on it is
wiped every `git push`. For the production service that is a data-loss warning
and `start.py` says so at CRITICAL. For the demo it is the design.

`start_demo.py` seeds on boot when the database is empty **or** when the newest
offer is older than `DEMO_MAX_AGE_HOURS`. That solves two problems with one
mechanism: the wiped disk refills itself, and the demo never goes stale — which
it otherwise would, because the generated window ends at the moment it was
seeded. A demo seeded in May is, by August, a board whose newest job is three
months old, shown to a prospect as evidence the product watches their work in
real time.

Cold start measured at about **45 seconds** from empty database to serving.
`railway.json` already allows a 300-second health-check window, so there is
room.

| Variable | Default | Meaning |
|---|---|---|
| `DEMO_SEED_ON_BOOT` | `true` | seed at startup at all |
| `DEMO_MAX_AGE_HOURS` | `20` | re-seed if the newest offer is older |
| `DEMO_WEEKS` | `13` | whole weeks of history to generate |
| `DEMO_FORCE_RESEED` | `false` | re-seed every boot, however fresh |

If you would rather the demo persist, attach a Postgres service and set
`DATABASE_URL` to it; boot-seeding then only fires on the first deploy and
whenever the data ages past the threshold.

### What it refuses to do

`start_demo.py` will not start against a roster whose default company is not
the demo tenant — pointing it at the production root exits with a message
naming what it found instead. The seeder additionally refuses a `DATABASE_URL`
that does not look like the demo database, and one that resolves outside the
demo root. Three separate checks, because the thing being guarded against is
writing thousands of synthetic offers into a real customer's board.

---

## Known limits

* **Locally, the demo does not update itself.** Its window ends at the moment
  it was seeded, so it ages. Re-run the seeder with `--reset` to refresh it.
  Deployed, `start_demo.py` handles this on boot — see above.
* **`--weeks 13` is one quarter,** which gives the monthly view three full
  months plus the current partial one. Fewer weeks and the monthly comparison
  has nothing to compare against.
* **The database is not committed** (`data/` is gitignored), so a fresh clone
  has the roster but no data until the seeder is run.
