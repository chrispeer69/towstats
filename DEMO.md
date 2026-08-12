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

## Known limits

* **The demo does not update itself.** Its window ends at the moment it was
  seeded, so it ages. Re-run the seeder to refresh it. Anything long-lived
  should run it on a schedule.
* **`--weeks 13` is one quarter,** which gives the monthly view three full
  months plus the current partial one. Fewer weeks and the monthly comparison
  has nothing to compare against.
* **The database is not committed** (`data/` is gitignored), so a fresh clone
  has the roster but no data until the seeder is run.
