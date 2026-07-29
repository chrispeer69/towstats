# Why the deployment files say what they say

Short notes on the four files at the repo root that only exist for hosting.
The step-by-step deploy instructions are in `README.md` -> **Deploying to
Railway**; this is the reasoning behind the settings, kept out of the README so
that document stays a set of instructions.

| File | Owns |
| --- | --- |
| `railway.json` | Railway's build and deploy settings, in the repo instead of the dashboard |
| `nixpacks.toml` | The Python version and the install commands |
| `Procfile` | The same process definitions in a provider-neutral format |
| `start.py` | The container entrypoint: check storage, migrate, cold-start backfill, serve |
| `.python-version` | The interpreter pin that local tooling reads |

## `railway.json`

**`numReplicas: 1`.** The scheduler runs inside the web process. Two replicas
means two schedulers pulling the Towbook API on the same cron. The advisory lock
in `towbook_agent/core/leader.py` stops the second one from scheduling, but a
deployment that only works because a lock is catching it is a deployment waiting
to surprise someone. Keep this at 1 unless you have first set
`RUN_SCHEDULER=false` and moved the scheduler to its own service.

**`healthcheckPath: /healthz`.** Exempted from the password gate in
`towbook_agent/web/auth.py`, so the platform can reach it without credentials.
It reports the database, the migration result and whether the scheduler is
running, which are the three things worth knowing before routing traffic.

**`healthcheckTimeout: 300`.** The first boot against an empty database runs
migrations and then pulls 30 days from Towbook before it serves (see
`BOOTSTRAP_ON_EMPTY`). That is a one-off and it is slow. Every later deploy
answers in under a second.

**`restartPolicyMaxRetries: 10`, not 3.** The most likely reason this process
dies at boot is a database that is not accepting connections yet — a Postgres
service that is still starting, or a brief network partition between the two.
Three retries can burn through that window and leave the service down until
somebody notices; ten rides it out. The process is designed not to exit on an
application-level failure at all (it comes up and shows a banner instead), so
retries are only ever spent on infrastructure.

## `nixpacks.toml`

Pins `python312`. `pyproject.toml` requires `>=3.11` and the wheels are verified
on 3.12; Nixpacks otherwise picks whatever it currently considers default, which
changes without notice and would eventually pick a version with no `psycopg`
wheel. `.python-version` carries the same number for pyenv and uv locally.

`gcc` is in the setup phase as a safety net only. `psycopg[binary]` and every
other dependency here ship wheels, so nothing should need to compile; if the
build ever starts compiling something, that is worth investigating rather than
papering over.

## `Procfile`

Provider-neutral duplicate of the start command, so this repo also deploys to
Render, Fly, Dokku or Heroku without editing anything. It defines a `worker`
process as well, which is **not** enabled by default — it is the two-service
layout, documented in the file itself, for when the pulls get heavy enough to
slow page rendering.

## `start.py`

Thin. Every decision it needs already exists in the package and it calls that
code rather than reimplementing it — see the module docstring for the mapping. A
second copy of the boot logic is a second copy that can be wrong, and the one
that runs only in production is the one nobody tests.

The single behaviour unique to it is the cold-start backfill: on a database with
no requests at all, pull the trailing 30 days once so the first deploy shows
real numbers instead of empty tabs. One acquisition for the whole window, then
metrics recomputed per day from the stored rows with no further network calls.
