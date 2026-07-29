# Why the deployment files say what they say

Short notes on the four files at the repo root that only exist for hosting.
The step-by-step deploy instructions are in `README.md` -> **Deploying to
Railway**; this is the reasoning behind the settings, kept out of the README so
that document stays a set of instructions.

| File | Owns |
| --- | --- |
| `railway.json` | Railway's build and deploy settings, in the repo instead of the dashboard |
| `nixpacks.toml` | One appended build package, and nothing else |
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

**It adds one package and overrides nothing.** This is a correction: the first
version of the file set `nixPkgs = ["python312", "gcc"]` and wrote its own
install commands, and it never built. In a Nixpacks phase an array is a
*replacement*, not an addition — that line discarded the Python provider's own
package list, and the Nix `python312` package ships no `pip`, so the build died
on its first install command:

```
/root/.nix-profile/bin/python: No module named pip
"python -m pip install --upgrade pip" did not complete successfully: exit code 1
```

`nixPkgs = ["...", "gcc"]` is the documented splice: `...` is a hole filled by
the values from the plan being merged into, so the provider keeps its packages
and `gcc` is appended.

**Do not fix a missing `pip` with `python -m ensurepip`.** It is the obvious
one-line repair and it fails twice over. The interpreter that `pip` would belong
to lives in `/nix/store`, which is read-only, so the install has nowhere to
write; and anything that did install would land outside `/opt/venv`, which is
the directory on `PATH` when the start command runs — a green build followed by
`ModuleNotFoundError` at boot. The provider creates that venv, installs
`requirements.txt` into it and puts it on `PATH`, which is why there is no
`[phases.install]` here any more and no `buildCommand` in `railway.json`.

**The interpreter is pinned in `.python-version`, not here.** Nixpacks reads it
(after `$NIXPACKS_PYTHON_VERSION`, ahead of `runtime.txt` and `.tool-versions`)
and pyenv and uv read it locally, so one line pins the version everywhere rather
than one number here drifting from another there. Nixpacks falls back to 3.11
when it finds nothing; `pyproject.toml` requires `>=3.11` and every wheel is
verified on 3.12. If a Nixpacks release ever stops reading the file, set
`NIXPACKS_PYTHON_VERSION=3.12` as a service variable — do not reinstate the
`nixPkgs` override.

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
