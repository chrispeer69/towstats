# Agero Deep Dive

**Roadside Towing and Recovery Inc — 30 days, Jun 29 – Jul 28, 2026**

---

## The number

**1,942 offers. 65% of everything you are sent. You won 710 — 36.6%.**

965 of the misses are yours to fix. At your configured $65/job that is roughly
**$62,700 a month**, of which **$46,700 is tow work**.

But the headline number is not the finding. This is:

| Agero | offers | accepted | unanswered |
| --- | ---: | ---: | ---: |
| **Inside** your staffed window (Mon–Fri 06:00–18:00) | 1,000 | **60%** | 6% |
| **Outside** it | 942 | **12%** | 63% |

You are not bad at Agero. You are **absent** after 18:00 and on weekends. Inside
your staffed window you accept 6 of every 10 Agero offers, which is a good
number. Outside it you accept 1 in 8.

---

## What the dashboard is currently telling you wrong

The board reports Allstate going unanswered 0.2% of the time against Agero's
33%, and flags it as a process difference — "same trucks, same staff." It is not
a process difference.

| Allstate | offers | accepted | canceled | expired |
| --- | ---: | ---: | ---: | ---: |
| Inside window | 283 | **67%** | 18% | 1 |
| Outside it | 383 | **11%** | **77%** | 0 |

Identical collapse: 67% → 11% for Allstate, 60% → 12% for Agero. The only
difference is the word the club writes down when you do not answer. **Agero says
*Expired*. Allstate says *Cancelled*.**

*Cancelled* lands in the `client_withdrew` bucket, and
`count_client_withdrew_as_recoverable: false` excludes that bucket from your
recoverable total. So roughly **294 Allstate offers a month are missed work
filed as "the client pulled it, not our fault."**

Agero looks like your problem client because Agero is the only one telling you
the truth.

Across all five clients, tow and winch only: **inside the window 65% accepted;
outside it, 13% accepted and 889 offers lost.**

---

## Where the money actually is

Weekends, not nights. Saturday and Sunday midday is the densest hole in the
data — 14:00, 15:00, 16:00 and 17:00 all run 95–100% missed at roughly 40 offers
per hour.

| Candidate shift | hrs/wk | tows missed /30d | missed per hour | at $65 |
| --- | ---: | ---: | ---: | ---: |
| **Sat+Sun 10:00–19:00** | 18 | 307 | **3.98** | $19,955 |
| Sat+Sun 06:00–22:00 | 32 | 423 | 3.08 | $27,495 |
| Mon–Fri 17:00–23:00 | 30 | 330 | 2.57 | $21,450 |
| Mon–Fri 18:00–22:00 | 20 | 247 | 2.88 | $16,055 |
| All week 22:00–06:00 | 56 | 219 | 0.91 | $14,235 |

Sunday currently accepts **1.7%** of tow offers — 3 of 174. Saturday, **7.9%**.

---

## Action plan, in order

### 1. Staff Saturday and Sunday, 10:00–19:00

Eighteen hours a week. **307 tow offers a month are dying in them.** Capture even
half and it is about $10,000 a month against one person's weekend wage. Nothing
else in this dataset returns four jobs per staffed hour.

### 2. Then Monday–Friday, 17:00–23:00

**330 more.** Note that 17:00–18:00 alone accounts for 123 missed tows: your
coverage ends exactly one hour before the evening peak, and 18:00 is the single
busiest offer hour of the day.

### 3. Close off light service

**431 offers a month producing 13 jobs — a 3% conversion on 14.4% of your total
volume.** And **63% of it lands in an hour where a tow was missed**, competing
for the same attention inside the same short decision window.

Ask Agero to stop sending:

| Service type | offers | jobs |
| --- | ---: | ---: |
| Tire Change | 110 | 0 |
| Battery Jump | 73 | 3 |
| Lock Out | 58 | 4 |
| Fuel Delivery | 18 | 0 |
| Accident Tow | 19 | 0 |

Ask Allstate the same for its 110 light-service offers, which produced 4 jobs.

### 4. Interrogate "Equipment Not Available"

**178 tow declines a month ($11,570), 125 of them plain *Tow* — your core
product.** 100 of the 178 fall outside staffed hours, and they spike at
16:00–19:00 (77 of them).

That pattern says *nobody there to send*, not *wrong truck class*. If dispatch is
using this reason as a catch-all, the equipment line on your board is fiction and
the staffing case above is larger than it already looks.

### 5. Fix the Allstate blind spot in the model

Treat "Cancelled outside staffed hours" as a non-response, or at minimum surface
it separately. As it stands the board hides roughly **$19,000 a month** of missed
Allstate work and prints a client comparison that points you at the wrong
problem.

---

## Three caveats to hold

- **Dollars are jobs × $65 from `job_value_by_client`, not revenue.** The
  `amount` field is `0` on all 2,996 records. Every figure here is a job count
  priced by your configuration.
- **The week of Jul 27 looks excellent — ignore it.** 60% accepted, 14%
  unanswered, but it is Monday and Tuesday only, 101 offers, no weekend yet. The
  weekend is what drags the average down.
- **`responded_at` is empty on every record**, so response latency cannot be
  measured. The "3-minute median decision window" cited in
  `MISSED_WORK_MODEL.md` is not verifiable from this data.

---

# Appendix — supporting detail

## A. Agero funnel, all service classes

| Bucket | offers | share |
| --- | ---: | ---: |
| Won | 710 | 36.6% |
| No response (expired) | 648 | 33.4% |
| Declined | 316 | 16.3% |
| Client withdrew | 267 | 13.7% |
| Accept failed | 1 | 0.1% |

Recoverable (declined + no response + accept failed): **965 = 49.7% of offers.**

## B. Agero by service class

| Class | offers | won | rate | unanswered | declined | withdrew |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| tow | 1,660 | 700 | 42.2% | 512 | 189 | 258 |
| light_service | 259 | 7 | 2.7% | 123 | 123 | 6 |
| winch_out | 22 | 2 | 9.1% | 13 | 4 | 3 |

Work you want (tow + winch_out): **1,682 offers, 702 won (41.7%), 719
recoverable ≈ $46,735/30d.** Of those, **525 were simply never answered**
(≈ $34,125/30d).

## C. Agero by hour of day — tow and winch only

Blind spot = 5 or more offers and 35%+ unanswered, per your `rules.yaml`.

| Hour | offers | won | unanswered | rate | |
| --- | ---: | ---: | ---: | ---: | --- |
| 00:00 | 35 | 11 | 10 | 28.6% | |
| 01:00 | 27 | 4 | 17 | 63.0% | **blind spot** |
| 02:00 | 14 | 1 | 8 | 57.1% | **blind spot** |
| 03:00 | 7 | 0 | 5 | 71.4% | **blind spot** |
| 04:00 | 20 | 4 | 13 | 65.0% | **blind spot** |
| 05:00 | 46 | 28 | 6 | 13.0% | |
| 06:00 | 93 | 65 | 9 | 9.7% | |
| 07:00 | 66 | 47 | 10 | 15.2% | |
| 08:00 | 78 | 51 | 8 | 10.3% | |
| 09:00 | 82 | 65 | 8 | 9.8% | |
| 10:00 | 106 | 69 | 11 | 10.4% | |
| 11:00 | 96 | 58 | 15 | 15.6% | |
| 12:00 | 104 | 56 | 16 | 15.4% | |
| 13:00 | 115 | 63 | 20 | 17.4% | |
| 14:00 | 107 | 51 | 23 | 21.5% | |
| 15:00 | 94 | 38 | 27 | 28.7% | |
| 16:00 | 95 | 22 | 26 | 27.4% | |
| 17:00 | 108 | 14 | 59 | 54.6% | **blind spot** |
| 18:00 | 113 | 20 | 64 | 56.6% | **blind spot** |
| 19:00 | 66 | 6 | 38 | 57.6% | **blind spot** |
| 20:00 | 76 | 14 | 51 | 67.1% | **blind spot** |
| 21:00 | 62 | 5 | 46 | 74.2% | **blind spot** |
| 22:00 | 38 | 2 | 20 | 52.6% | **blind spot** |
| 23:00 | 34 | 8 | 15 | 44.1% | **blind spot** |

Sixty hour-of-week cells qualify as blind spots, accounting for **438 lost jobs**
(≈ $28,470/30d). The worst: Sun 18:00 (15 of 16 missed), Sat 18:00 (15 of 21),
Sun 15:00 (14 of 15), Sun 14:00 (13 of 13), Sat 16:00 (13 of 13).

## D. Agero by day of week — tow and winch only

| Day | offers | won | rate | unanswered | rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| Mon | 347 | 180 | 51.9% | 44 | 12.7% |
| Tue | 261 | 158 | 60.5% | 45 | 17.2% |
| Wed | 253 | 130 | 51.4% | 44 | 17.4% |
| Thu | 252 | 121 | 48.0% | 56 | 22.2% |
| Fri | 218 | 96 | 44.0% | 54 | 24.8% |
| **Sat** | 177 | 14 | **7.9%** | 126 | 71.2% |
| **Sun** | 174 | 3 | **1.7%** | 156 | 89.7% |

## E. Weekend hour by hour — all clients, tow and winch

| Hour | offers | accepted | missed | missed % |
| --- | ---: | ---: | ---: | ---: |
| 06:00 | 12 | 0 | 12 | 100% |
| 07:00 | 15 | 1 | 13 | 87% |
| 08:00 | 19 | 4 | 11 | 58% |
| 09:00 | 20 | 1 | 16 | 80% |
| 10:00 | 19 | 1 | 17 | 89% |
| 11:00 | 39 | 1 | 32 | 82% |
| 12:00 | 40 | 3 | 31 | 78% |
| 13:00 | 30 | 0 | 28 | 93% |
| 14:00 | 38 | 0 | 38 | 100% |
| 15:00 | 40 | 0 | 40 | 100% |
| 16:00 | 42 | 0 | 41 | 98% |
| 17:00 | 40 | 0 | 38 | 95% |
| 18:00 | 49 | 4 | 42 | 86% |
| 19:00 | 31 | 2 | 22 | 71% |
| 20:00 | 23 | 0 | 22 | 96% |
| 21:00 | 22 | 1 | 20 | 91% |
| 22:00 | 16 | 2 | 14 | 88% |

"Missed" here counts expired offers plus offers cancelled outside staffed hours —
see the Allstate note above.

## F. Why you said no — Agero declines

| Reason | declines | of which tow/winch |
| --- | ---: | ---: |
| Equipment Not Available | 239 | 137 |
| No Drivers Available | 30 | 19 |
| Other | 25 | 18 |
| Out of Service Area | 17 | 16 |
| Not Enough Information | 5 | 3 |

## G. Client comparison — tow and winch only

| Client | offers | won | rate | unanswered | rate | declined |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Agero (Swoop) | 1,682 | 702 | 41.7% | 525 | 31.2% | 193 |
| Allstate | 556 | 227 | 40.8% | 1 | 0.2% | 40 |
| NSD | 190 | 68 | 35.8% | 68 | 35.8% | 0 |
| Urgent.ly | 67 | 26 | 38.8% | 18 | 26.9% | 5 |
| Allied Dispatch Solutions | 63 | 28 | 44.4% | 22 | 34.9% | 0 |

Allstate's 0.2% is the reporting artifact described above, not a better result.

## H. Trend by week — Agero, tow and winch

| Week of | offers | won | rate | unanswered | rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| Jun 29 | 437 | 153 | 35.0% | 170 | 38.9% |
| Jul 06 | 389 | 176 | 45.2% | 102 | 26.2% |
| Jul 13 | 392 | 172 | 43.9% | 100 | 25.5% |
| Jul 20 | 363 | 140 | 38.6% | 139 | 38.3% |
| Jul 27 *(partial — 2 weekdays)* | 101 | 61 | 60.4% | 14 | 13.9% |

## I. Client withdrawals — Agero, tow and winch

261 offers Agero pulled back, 15.5% of the work you want.

| Raw status | count |
| --- | ---: |
| Cancelled | 174 |
| Rejected By Motor Club | 38 |
| Service Failure Confirmed | 25 |
| GOA Approved By Motor Club | 24 |

---

*Analysis run against the 30-day backfill in `data/towbook.db`, Jun 29 – Jul 28
2026, using this company's own bucket, coverage and blind-spot rules from
`config/rules.yaml` and `config/companies.yaml`. Timezone America/Detroit.*
