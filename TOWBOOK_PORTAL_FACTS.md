# Towbook Portal — Verified Facts

Everything in this file was confirmed **live against the real Towbook account** on
2026-07-28, not inferred. The build document listed these as "KNOWN UNKNOWNS"; they are
now known. Treat this file as the source of truth for `config/schema.yaml`,
`config/selectors.yaml` and `config/rules.yaml`.

Account observed: **Roadside Towing and Recovery Inc**, `companyId = 61343`.

---

## 1. Authentication — CONFIRMED WORKING

| Item | Value |
|---|---|
| Login URL | `https://app.towbook.com/Security/Login` |
| Method | `POST`, standard form encoding |
| Username field | `id="Username"` `name="Username"` |
| Password field | `id="Password"` `name="Password"` |
| Submit button | `<button type="submit" name="bSignIn">Log in</button>` |
| Antiforgery form field | `name="RequestVerificationToken"` (hidden) |
| Antiforgery cookie | `.AspNetCore.Antiforgery.<random>` |
| **Session cookie issued on success** | **`.xtl`** (~520 chars, HttpOnly) |
| Secondary cookie | `X-Session-Timeout` |
| On success, redirects to | `https://app.towbook.com/` |
| CAPTCHA / bot wall | **None** |
| MFA | **None** on this account |

Unauthenticated requests to any path redirect to
`/Security/Login?ReturnUrl=<path>`.

**Login success test:** an auth cookie named `.xtl` is present AND the final URL is not
`/Security/Login`. Do **not** test for `.AspNetCore.Cookies` — Towbook does not use it.

**Login failure test:** still on `/Security/Login` after POST, and the response contains
`field-validation-error` / a populated `data-valmsg-for="Username"` or `"Password"` span.

---

## 2. Navigation — CONFIRMED

Top nav is `ul#root.towbook-top-navigation`:

| Label | href |
|---|---|
| Dashboard | `/Index` |
| **Dispatching** | **`/DS4/`** |
| Map | `/Map/` |
| Impounds | `/Impounds/` |
| Accounts | `/Accounts/` |
| Reports | `/Reports/` |
| Settings | `/Settings/` |
| Log Out | `/security/logout?id=<token>` |

**Request Log lives at `/requestLog/`** (linked from `/DS4/`). It is a
[w2ui](https://w2ui.com) grid, not a server-rendered table — the HTML contains no data
rows, so DOM scraping requires JavaScript execution.

Request Log UI controls (ids confirmed in page source):

| Control | Selector |
|---|---|
| Start date | `#startDate` (jQuery UI datepicker, `mm/dd/yyyy`) |
| End date | `#endDate` (jQuery UI datepicker, `mm/dd/yyyy`) |
| Page size | `#pageSizeSel` (`<select onchange="changePageSize(this)">`) |
| Grid | `w2ui.digitalGrid` |
| Export | w2ui built-in, `toolbarExport: true` |

---

## 3. The JSON API — PREFERRED ACQUISITION PATH

The grid is fed by a JSON endpoint. Using it directly is **far more reliable than
driving the UI and exporting XLSX**: no browser, no download handling, no DOM breakage,
no header drift.

```
GET https://app.towbook.com/api/digitaldispatch/callrequests
      ?extended=true
      &page=<1-based>
      &pageSize=<max 1000>
      &startDate=<YYYY-MM-DD or MM/DD/YYYY>
      &endDate=<YYYY-MM-DD or MM/DD/YYYY>
```

Headers: `Referer: https://app.towbook.com/requestLog/`,
`X-Requested-With: XMLHttpRequest`. Auth via the `.xtl` session cookie.

**Verified behaviour**

- Total row count returned in the **`X-Records-Count`** response header.
- `pageSize` **maximum is 1000**. `pageSize=2000` returns **HTTP 500** after a ~30 s
  server-side timeout. Always cap at 1000.
- Pagination is clean and stable: 30-day window, `X-Records-Count = 3079`,
  pages 1–4 at `pageSize=1000` returned 1000/1000/1000/79 = **3079 distinct ids,
  zero overlap**. Page beyond the end returns `[]`.
- Both `YYYY-MM-DD` and `MM/DD/YYYY` date formats are accepted.
- `endDate` is **inclusive of that calendar day**. Same-day (`start == end`) works and
  its result set is a strict subset of the wider range.
- Filtering is **by date only — there is no time-of-day filter.** For hourly reporting,
  pull the calendar day and bucket by `requestDate` locally.
- With no date filter the account holds **156,442 total records** — deep history is
  available for backfill.

---

## 4. Record schema — REAL FIELD NAMES

These replace the guessed XLSX headers.

| API field | Type / note | Canonical field |
|---|---|---|
| `callRequestId` | **unique** (3079/3079 distinct) | `request_id` |
| `providerName` | the motor club offering the job | `client_name` → `client_key` |
| `requestDate` | **company-local time**, e.g. `2026-07-28T10:31:39.71` | `offered_at` |
| `requestDateUtc` | **always `0001-01-01T00:00:00` — never populated. Do not use.** | — |
| `status` | numeric code, see §5 | `status` (via vocabulary) |
| `statusName` | human label for `status` | — |
| `responseReasonName` | decline reason, blank when accepted | `denial_reason` |
| `serviceNeeded` | **verbatim service type — never mutate** | `service_type_raw` |
| `startingLocation` | full street address | `pickup_location` |
| `towDestination` | full street address | `dropoff_location` |
| `offerAmount` | numeric, frequently `0.0` | `amount` |
| `ownerUserName` | user who responded | (no canonical field; keep as extra) |
| `vehicle` | `"2012 HONDA ODYSSEY EX red"` | (extra) |
| `expirationDate` | offer expiry | (extra) |
| `companyId` / `companyName` | 61343 / Roadside Towing and Recovery Inc | `account_id` |
| `contractorId`, `accountId`, `masterAccountId` | ids | (extra) |
| `callNumber`, `purchaseOrderNumber` | may be `0` / blank | (extra) |
| `distance`, `zip`, `etaGiven`, `maxEta` | numeric | (extra) |
| `reason` | free text *problem* description (`"Dead battery"`) — **not** the decline reason | (extra) |
| `availableActions` | array, e.g. `["ACCEPT","REJECT"]` | (extra) |
| `supportedEtas` | array of ints | (extra) |

> **`responded_at` has no source field in this API.** Leave it null, or infer it later
> from a dispatch-entry lookup. Documented as an assumption, not silently faked.

---

## 5. Status codes — REAL VALUES

The build document assumed a 5-value vocabulary. The portal actually emits **14 codes**.
This is exactly why status mapping lives in `schema.yaml` rather than in code.

| Code | `statusName` | Suggested canonical | Note |
|---:|---|---|---|
| 1 | Accepted | `accepted` | |
| 2 | Rejected | `denied` | we declined |
| 4 | Cancelled | `canceled` | |
| 5 | Expired | `expired` | offer timed out |
| 6 | Accepting | `pending` | in flight |
| 7 | Rejecting | `pending` | in flight |
| 10 | Accept Sent | `accepted` | |
| 21 | Accept Failed | `expired` | we tried to accept and lost it |
| 22 | Reject Failed | `denied` | |
| 40 | Rejected By Motor Club | `canceled` | client withdrew |
| 41 | Service No Longer Needed | `canceled` | |
| 71 | Goa Approved By Motor Club | `canceled` | gone on arrival |
| 80 | Another Provider Responded | `expired` | we lost it to a competitor |
| 90 | Service Failure Confirmed | `canceled` | |

Codes 21/80 matter: they are **lost work, not owner declines**, and must not inflate the
denial rate. Keep them distinguishable — `status_vocabulary` maps them, and
`service_type_raw` + `statusName` are retained so the split can be revisited without a
migration.

---

## 6. Real service types (30-day sample, 3,079 requests, 39 distinct)

```
1071 Tow                        21 Light Lock-out          3 Fuel
 329 Light Tow                  18 Accident Tow            2 Winch
  82 Flat Bed Towing            15 Winch Out               2 Light Duty Unleaded Fuel Delivery
  76 Tire Change                12 Jump Start              2 Tire Inflation
  50 Battery Jump               12 Light Start             1 Start
  46 Light Duty Towing           7 Light Accident Tow      1 Parts Delivery + 1hr Labor
  46 Tow / Flatbed               7 Fuel Delivery           1 Light Standard Tow
  42 Light Tire Change           5 Flat Tire               1 Low Clearance Tow
  37 Lock Out                    4 Lockout                 1 Heavy Duty TOW
  36 Towing                      4 Flat Bed Accident       1 Salvage Tow
  33 Accident Tow (P)            4 Light Fuel Delivery     1 Auto Lockout
  23 Light Secondary Simple Tow  3 Medium Duty TOW         1 Winching  … + Light Winch
```

> "Light" here means **light-duty vehicle class, not light service**. `Light Tow`,
> `Light Duty Towing`, `Light Accident Tow` etc. are **tows** and correctly classify as
> `tow` because they contain the substring `tow`. This is load-bearing — do not "fix" it.

### Gaps found in the document's shipped rules

Applying the document's `rules.yaml` verbatim to real traffic leaves **61 rows (2.0%)
unclassified**:

`Light Lock-out` (25), `Light Start` (20), `Start` (6), `Fuel` (3), `Winch` (2),
`Tire Inflation` (2), `Parts Delivery + 1hr Labor` (1), `Winching` (1), `Light Winch` (1)

Adding these terms drops it to **7 rows (0.2%)**:

- `winch_out.match_any` — add bare **`winch`** (covers Winch, Winching, Light Winch,
  and still matches Winch Out)
- `light_service.match_any` — add **`lock-out`**, **`light start`**, **`tire inflation`**,
  bare **`fuel`**, **`unleaded`**
- `tow.match_any` — add **`flat bed`** (two words) and **`salvage`**

`Start` (6) and `Parts Delivery + 1hr Labor` (1) are left deliberately unclassified —
they are genuinely ambiguous and should surface for the owner to decide. That is the
`_default: unclassified` mechanism working as designed.

---

## 7. Denial reasons — need normalization

Real values, with obvious duplicates:

```
355 Equipment Not Available     18 Out of Service Area
 68 No Drivers Available         8 Out Of Coverage Area
 27 Other                        7 Equipment Availability
 23 Refuse                       5 Not Enough Information
                                 1 Out of Area
```

Normalization groups for `rules.yaml → denial_reason_normalization`:

- **out_of_area** ← Out of Service Area, Out Of Coverage Area, Out of Area
- **equipment_unavailable** ← Equipment Not Available, Equipment Availability
- **no_drivers** ← No Drivers Available
- **other** ← Other, Refuse, Not Enough Information

---

## 8. Baseline numbers (30 days, corrected rules)

| Service class | Offered | Accepted | Rate |
|---|---:|---:|---:|
| tow | 2,598 | 1,023 | **39.4%** |
| winch_out | 27 | 3 | 11.1% |
| light_service | 447 | 13 | 2.9% |
| unclassified | 7 | 1 | 14.3% |

Clients: Agero (Swoop) 1,289 · Allstate 440 · NSD 159 · Urgent.ly 60 ·
Allied Dispatch Solutions 52.

Policy variance: **1,575 tows offered but not accepted**; **13 light-service jobs
accepted against policy**. Note that the 1,575 includes Expired / Another Provider
Responded / Cancelled — the daily report must break these out by reason rather than
reporting them all as owner declines.

---

## 9. Acquisition strategy

1. **Primary — JSON API.** Authenticate with `httpx`, then page `callrequests` at
   `pageSize=1000`. No browser required. Fast (~1.5 s per 1000 rows), deterministic,
   and immune to DOM changes.
2. **Fallback — Playwright UI export.** Kept because the build document specifies it and
   because it is the escape hatch if Towbook changes or revokes the JSON endpoint.
   Selectors live in `config/selectors.yaml`; run `discover-selectors` to refresh them.

Both paths archive their raw payload under `raw/YYYY/MM/DD/` and never delete.
