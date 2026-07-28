# Deploying to Railway

Do these in order. Step 2 is not optional — skipping it means your data is
deleted every time you deploy.

---

## 1. Create the service

Railway → **New Project** → **Deploy from GitHub repo** → `chrispeer69/towstats`.

It will start building immediately and **the first build will fail** until you
finish step 3. That is expected.

## 2. Add Postgres — required

**New** → **Database** → **Add PostgreSQL**, in the same project.

Railway containers have an **ephemeral filesystem**. A SQLite file lives inside
the container, so every redeploy destroys it and every report silently resets to
empty. Postgres is a separate service with a real disk, so it survives.

Once added, Railway exposes `DATABASE_URL` to the project. The app rewrites the
`postgres://` and `postgresql://` schemes to the driver form automatically — you
do not need to edit it.

## 3. Set variables

Service → **Variables**:

| Variable | Value | Notes |
|---|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` | Reference the Postgres service |
| `TOWBOOK_USER` | your Towbook username | Required |
| `TOWBOOK_PASS` | your Towbook password | Required |
| `DASHBOARD_PASSWORD` | `1234` | Password for the board |
| `SESSION_SECRET` | any long random string | Keeps you logged in across restarts |
| `TZ` | `America/Detroit` | Your local time — every window depends on it |
| `LOG_LEVEL` | `INFO` | |
| `BOOTSTRAP_DAYS` | `30` | Days to load on a cold database |
| `RUN_SCHEDULER` | `true` | Keeps the board updating |

`PORT` is supplied by Railway. Do not set it.

## 4. Deploy

Push to `main`, or hit **Redeploy**. On boot the app:

1. normalises `DATABASE_URL`
2. runs `alembic upgrade head`
3. **if the database is empty**, pulls the last 30 days from Towbook
4. serves on `$PORT`

The first boot takes several minutes — it is fetching 30 days one day at a time.
Watch **Deploy Logs**; you will see `bootstrap complete: 31 of 31 days loaded`.

## 5. Open it

Service → **Settings** → **Networking** → **Generate Domain**. That gives you a
`*.up.railway.app` URL. Open it, log in with `DASHBOARD_PASSWORD`.

---

## Troubleshooting

**Build fails immediately.** Almost always a missing start command. This repo
ships `Procfile`, `railway.json` and `nixpacks.toml`; if Railway ignores them,
set **Settings → Deploy → Start Command** to `python start.py` manually.

**Board is empty.** Either `TOWBOOK_USER`/`TOWBOOK_PASS` are unset, or the
bootstrap failed. Search the deploy logs for `bootstrap`. You can also load a
single day by hand:

```
python -m towbook_agent run --report daily --date 2026-07-27
```

**"DATABASE_URL is not set" in the logs.** The Postgres service is not attached.
The app falls back to SQLite so it can still serve a page and tell you this, but
that data will not survive the next deploy. Go back to step 2.

**Numbers look wrong.** Open the **Health** view first: it shows the last
successful pull per report type, row counts, and any failure with its diff.

**Everything worked, then went stale.** Check `RUN_SCHEDULER` is `true`. There is
no SMS fallback — the board is the only delivery mechanism, so a stopped
scheduler is silent. Any failed or overdue run raises a banner on every tab.

---

## Cost

One web service plus one Postgres. The bootstrap makes ~31 API calls to Towbook
once, then roughly one call an hour. Well inside Railway's smallest paid tier.

## Security

`DASHBOARD_PASSWORD=1234` is a single shared password with no user accounts and
no audit trail. That is fine for one owner checking his own numbers. It is
**not** adequate once several towing companies' customer data sits behind it —
addresses, vehicles and job values are commercially sensitive. Before sharing
this with other US Tow Alliance members, give each company its own credentials.
