# Operations log

Days where something outside the numbers explains the numbers. Weather, road
closures, a truck down, a dispatcher out, a client changing how they dispatch.

**Why this file exists.** The system measures what was offered and what was
accepted. It cannot see *why* a day went the way it did, and a day with a real
external cause looks identical in the data to a day the desk simply performed
badly. Read this file before writing or defending any report that covers a day
listed here.

Nothing in this file is read by the code. It is written for the person holding
the report.

Newest first. Convert every relative date to an absolute one.

---

## 2026-08-11 (Tuesday) — Central Ohio storms

**What happened.** Major rain storms, extremely high winds, and flooding across
Central Ohio. Onset approximately **10:30 AM local**, continuing the rest of the
day.

**Effect on the numbers.** Offer volume rose sharply and stayed elevated. There
was not enough manpower to keep up, so a large share of the day's offers went
unanswered or expired. Expect for this day:

- Offered count well above a normal Tuesday
- Acceptance rate well below the Tuesday baseline
- Missed-work dollars well above normal, concentrated after 10:30

**How to read it.** The low acceptance rate on this day is a *capacity* result,
not a desk-performance result. The trucks and the people were already committed.
Do not present it as a dispatch failure, and do not quietly drop it either — see
below.

**Why it is worth more than an excuse.** This is the coverage argument the
system exists to make, in its strongest form: demand moved and staffing could
not, and the gap has a dollar figure attached to it. A day like this is the
evidence for what a surge plan or an on-call tier would have been worth. Use the
missed-work figure for the hours after 10:30 as the size of the opportunity, and
say plainly that it was a storm — the argument is stronger with the cause named,
not weaker.

**Baseline contamination — the part that outlives the day.**
`agents/morning_report.py -> same_weekday_baseline()` compares each day against
the **4 most recent same-weekday days that had offers**. This was a Tuesday, so
it sits in the Tuesday baseline for the next four Tuesdays:

> **2026-08-18, 08-25, 09-01, 09-08**

On each of those days the printed "4-Tue avg" is pulled toward a storm day, so a
normal Tuesday will read as an improvement it did not earn. Say so on those
reports rather than letting the comparison stand unexplained.

It also lands inside the **August monthly report** (runs 06:00 on 2026-09-01,
covering 2026-08-01 to 08-31). One storm day in a 31-day month moves the monthly
acceptance rate and the monthly missed-work total. Name it in that report.
