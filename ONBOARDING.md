# Onboarding a towing company

What to collect, what to set up, and how to check it worked. One pass takes
about 30 minutes, most of which is waiting for the first pull to finish.

---

## 1. What to collect from the customer

### Required — the system does not work without these

| What | Why | Notes |
|---|---|---|
| **Towbook username + password** | The agent signs into `app.towbook.com` as them and pulls the Digital Requests feed | **This is their Towbook portal login, not a database login.** No IT involvement, no database access, nothing to install on their end |
| **Towbook company id** | Which company inside the account to report on | Visible in the portal URL, or the book icon in the top right if they run more than one entity. Ask for one id per entity they want reported |
| **Legal / trading name** | Goes on the dashboard and the printed report header | Ask for both if they differ |
| **Timezone** | A coverage window read in the wrong zone is a claim about a shift nobody works | IANA form (`America/Detroit`, `America/Chicago`) |
| **Staffed hours** | The covered / uncovered split is the headline of every report and is wrong if this is wrong | Days and start/end times. Ask what they *actually* staff, not what the sign says |
| **Average job value per client** | Every dollar figure comes from this table. Towbook sends no amounts | GROSS revenue per completed job, keyed by motor club: Agero, Allstate, NSD, HONK, Urgent.ly, Allied, etc. Only the clients that dispatch to them |

### Ask for, but can start without

- Address, phone, email, website — printed letterhead only
- A logo file (PNG, under ~320px wide)
- Names and emails of everyone who needs a login
- Whether any client should be excluded from the numbers

### Do not ask for

- Database credentials. There is no database to connect to
- Anything from their accounting system. Job values are a number they tell you

### Before you take the login

A Towbook login is full access to their live dispatch system. Two things worth
doing:

1. **Ask for a limited sub-user** if their Towbook plan supports one. Read
   access to the Request Log is all this system needs
2. **Get authorization in writing** — one paragraph saying they authorize you
   to access their Towbook account to produce reporting, and that they can
   revoke it at any time. Keep it on file

---

## 2. Stand up the tenant

### 2a. Add them to the roster

Edit `config/companies.yaml`, copy the `example-towing` block, and fill it in:

```yaml
  - id: acme-towing              # NEVER changes once data exists for it
    name: Acme Towing & Recovery
    towbook_company_id: "254467" # load-bearing if they have >1 entity
    credentials_env: ACME        # -> TOWBOOK_ACME_USER / TOWBOOK_ACME_PASS
    timezone: America/Chicago
    enabled: true

    letterhead:
      name: ""
      address: "…"
      city: "…"
      state: "…"
      zip: "…"
      phone: "…"

    coverage:                    # their real staffed window
      windows:
        - name: covered
          days: [mon, tue, wed, thu, fri]
          start: "07:00"
          end: "19:00"
      default_label: uncovered

    job_value_by_client:         # casefolded client name -> gross dollars
      agero (swoop): 80
      allstate: 75
```

`coverage` and `job_value_by_client` **replace** the global defaults rather than
merging into them. List every client that dispatches to them, not just the ones
that differ.

### 2b. Set their credentials

Railway → the service → Variables:

```
TOWBOOK_ACME_USER=...
TOWBOOK_ACME_PASS=...
```

Never in `companies.yaml` — that file is committed to git. A company with a
`credentials_env` prefix deliberately does **not** fall back to
`TOWBOOK_USER` / `TOWBOOK_PASS`; a missing variable is a loud error rather than
a silent sign-in as the wrong company.

### 2c. Deploy and let it backfill

Commit the roster change and push. On boot the app migrates, then pulls the
trailing 30 days for any company with no stored requests
(`BOOTSTRAP_ON_EMPTY=true`). Watch the deploy log for the acquisition line.

### 2d. Create their logins

Sign in as an operator → **Accounts** → Add an account:

- One account **per person**, not per company. Two people at Acme get two
  logins, so the sign-in record means something and one of them leaving does
  not take the other's access with it
- Role **Member**
- Tick only their companies
- Pick a first password of at least 12 characters

Give them the password once, by phone. The board forces them to replace it
before it shows them a single figure, so it does not matter that it travelled.

> **If this install still has no accounts**, it is on the shared password and
> every reader can see every company. Create an operator account first — the
> Accounts screen is reachable with the shared password exactly once, and the
> first account you create ends that. Alternatively set `DASHBOARD_ADMIN_USER`
> and `DASHBOARD_ADMIN_PASS` and redeploy.

---

## 3. Verify before you hand it over

Sign in **as their account**, not as the operator — the operator sees
everything and will not catch a scoping mistake.

- [ ] The company switcher shows their companies and **nothing else**
- [ ] `/company/<some-other-tenant-id>` typed into the address bar lands back
      on one of theirs
- [ ] **Hourly** and **Daily** show real offers with real client names
- [ ] The client list matches the motor clubs they told you about — an
      unexpected name means a missing `job_value_by_client` entry
- [ ] **Missed work** dollar figures are non-zero and in the right ballpark
- [ ] The covered / uncovered split matches their staffed hours. If nearly
      everything is "uncovered", the window or the timezone is wrong
- [ ] **Health** shows a recent successful run and no red banner
- [ ] Print any tab → their letterhead, not yours
- [ ] Their account is prompted to change its password on first sign-in

---

## 4. Hand over

Send them:

- The board URL
- Their username; the password by phone, separately
- One sentence on what to look at first — usually the missed-work dollar figure
- Who to contact when a number looks wrong

Tell them plainly what it does with their Towbook login: reads the Digital
Requests feed on a schedule, writes nothing back to Towbook, and can be revoked
by changing their Towbook password.

---

## 5. Things that go wrong

| Symptom | Cause |
|---|---|
| Empty board after deploy | Credentials wrong, or `TOWBOOK_<PREFIX>_USER` misspelled. Check the deploy log — the error names the missing variable |
| Numbers belong to the wrong entity | `towbook_company_id` is wrong. It is what the session is switched to on a multi-entity account |
| Everything reads "uncovered" | Timezone or staffed window wrong. Check `timezone:` before touching `coverage:` |
| Client shows $0 of missed work | That client is not in `job_value_by_client`. Nothing is ever inferred |
| Their history vanished after an edit | `id:` was changed. Every stored row still carries the old one, and there is no rename path. Change it back |
| They can see another company | Their account is scoped to more than it should be, or they were made an operator. Check the Accounts screen |
