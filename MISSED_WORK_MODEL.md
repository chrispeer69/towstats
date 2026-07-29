# The Missed Work Model

The build document framed this system around acceptance rate. The owner's actual
question is narrower and more useful:

> **What are we not accepting? Do we want it? If yes, what do we need to do to accept it?
> If no, how do we stop it being offered at all?**

Acceptance rate is a symptom. The deliverable is an **inventory of missed work, attributed
to a cause, with an action attached.** This document defines that model. It is the spec
for `agents/missed_work.py`, the `missed_work` YAML block, and the dashboard's primary view.

---

## 1. Every offer lands in exactly one bucket

Derived from the `status` code (see `TOWBOOK_PORTAL_FACTS.md` §5). Buckets are defined in
`config/rules.yaml → missed_work.buckets` so they can be re-cut without a code change.

| Bucket | Status codes | Meaning | Ours to fix? |
|---|---|---|---|
| `won` | 1, 10 | Accepted | — |
| `in_flight` | 6, 7 | Still deciding | — |
| `declined` | 2, 22 | **We said no**, with a reason | **Yes** |
| `no_response` | 5, 80 | **Nobody answered in time** | **Yes** |
| `accept_failed` | 21 | We tried to accept and it failed | **Yes — technical** |
| `client_withdrew` | 4, 40, 41, 71, 90 | Client pulled it | Partly |

### The `client_withdrew` judgement call

Some withdrawals are genuinely the client's doing. Others are a client giving up because
**we** were slow — indistinguishable in this feed. Rather than hardcode a guess:

```yaml
missed_work:
  count_client_withdrew_as_recoverable: false   # default: report separately
```

Default `false` — it is reported as its own line, not folded into the recoverable total,
so the headline number is defensible. Set `true` to include it. Reports always show both,
so the choice never hides a number.

---

## 2. Root cause and remedy

`declined` rows carry `denial_reason`. Each reason maps to a **cause class** and a
**remedy class**. Configured in `rules.yaml → missed_work.remedies`, not in code.

| Cause | Real reasons seen | Remedy class | The question it answers |
|---|---|---|---|
| `equipment` | Equipment Not Available, Equipment Availability | capital | *Which truck class is missing?* |
| `staffing` | No Drivers Available | scheduling | *Which hours are uncovered?* |
| `coverage` | Out of Service Area, Out Of Coverage Area, Out of Area | territory | *Where, and how far out?* |
| `information` | Not Enough Information | process | *Cheap fix — ask before declining* |
| `review` | Other, Refuse | review | *Reason not captured — needs a human* |
| `unrecorded` | blank | data | *We declined without recording why* |

`no_response` has no reason field by definition — its cause is always **attention**, and its
remedy is **alerting/coverage during a specific window**. That is what §4 quantifies.

---

## 3. The recoverable inventory

For each `(service_class, cause)` pair, per period:

```
offers, accepted, missed, missed_share,
cause_breakdown{}, top_clients[], top_service_types[],
recoverable            # missed, excluding client_withdrew unless configured in
remedy                 # from rules.yaml
```

Ranked by **job count**, descending, filtered to `service_class` in
`acceptance_policy.should_accept` — because the inventory is about *work we want*.

> **Ranking is by job count, not revenue.** `offerAmount` is empty on 100% of records in
> this account. To rank by dollars the system needs either invoice data or a per-client
> average job value, configurable at `missed_work.job_value_by_client`. Until that is set,
> every report states that it is counting jobs, not money. It must never imply otherwise.

### Per company

Every threshold and every window in this document is **per towing company**.
`config/rules.yaml` is the default; a company entry in `config/companies.yaml` may replace
its `coverage` window and its `job_value_by_client` table outright, and deep-merge anything
else under `rules:`. The two that matter:

* **`coverage`** — §7's inside/outside split is a claim about whether a human was at a desk,
  so it is only meaningful in that company's own staffed hours *and* its own timezone. A
  company in Texas and one in Ohio measured against one window are both being measured
  against a shift neither of them works.
* **`job_value_by_client`** — replaces rather than merges, so a company that lists two
  clients does not silently inherit the other three from `rules.yaml` and price work it has
  never been offered.

Precedence, later winning: `rules.yaml` → `coverage:` → `job_value_by_client:` → `rules:`.
See the Multiple companies section of README.md.

---

## 4. Blind spots — when we fail to respond

A 7 × 24 hour-of-week grid. Each cell:

```
offers, accepted, no_response, no_response_rate
```

A cell is a **blind spot** when `offers >= min_offers` and
`no_response_rate >= threshold` (both in `rules.yaml → missed_work.blind_spot`,
defaults 5 and 0.35).

This is the highest-value output in the system. On 30 days of real data it isolates
**17:00–21:00 and 01:00–04:00** as the windows where offers go unanswered, while
08:00–13:00 sits at 9–15%. That is an evidence-backed staffing conversation.

Context that makes it urgent: **the median response window Towbook allows is 3 minutes**
(mean 4, max 15). A missed notification is a lost job almost immediately.

---

## 5. Close-off candidates — work we do not want

Service types we systematically refuse are noise: they consume attention inside a
3-minute decision window that tow offers are competing for.

A service type is a close-off candidate when, over the window:

```
offers >= min_offers            (default 5)
acceptance_rate <= max_rate     (default 0.10)
```

Output is grouped **by client**, because the action is a conversation with that client:
"stop sending us these". Each row carries service type, offers, accepted, rate, and the
share of that client's total offers it represents.

On real data this surfaces Tire Change (145 offers, 0 accepted), Battery Jump (77, 3),
Lock Out (61, 4), Light Tire Change (59, 1), Fuel Delivery (18, 0) — roughly 450 offers
producing 13 jobs.

**Closing these off is not only noise reduction — it is expected to improve the tow
response rate**, because it removes competing offers from the same 3-minute window. The
report states that as the rationale, and the system tracks whether it holds by monitoring
`no_response_rate` before and after.

---

## 6. Client comparison

Per client, per period: `offers, accepted, rate, declined, no_response, withdrew`, plus
`no_response_rate`.

Its purpose is to expose *inconsistency between clients that should behave alike*. On real
data, Allstate's offers go unanswered 0.1% of the time while Agero's do 34% — same trucks,
same staff. A gap that large is a process difference worth finding, and is surfaced as an
explicit finding, not left for the reader to spot.

---

## 7. What the reports lead with

Every report leads with missed work. Acceptance rate is supporting context.

**Hourly SMS** keeps the exact documented shape (it replaces a person texting these
numbers) and appends one missed-work line only when there is something to act on:

```
14:00-14:59 | Offered 12 / Accepted 9 (75%)
Day: 84 / 61 (73%)
!! 3 tows unanswered this hour
```

**Daily** leads with the recoverable inventory, then cause breakdown, then blind spots,
then close-off candidates, then acceptance rate.

**Weekly** adds trend: is each cause growing or shrinking, are blind spots moving, did a
close-off actually take effect.

---

## 8. Alerts

Added to `rules.yaml → alerts`, evaluated by the existing sandbox:

| id | when | severity |
|---|---|---|
| `tow_unanswered` | `service_class == 'tow' and bucket == 'no_response'` | high |
| `blind_spot_forming` | `hour_no_response_rate >= 0.35 and hour_offers >= 5` | high |
| `decline_cause_spike` | `cause_count_24h >= cause_baseline_7d * 2 and cause_count_24h >= 5` | medium |
| `closeoff_candidate` | `service_type_offers_7d >= 10 and service_type_rate_7d <= 0.10` | low |

`missed_tow` from the original document is retained; `tow_unanswered` is deliberately
separate and higher severity, because "nobody answered" is a different failure from
"we consciously declined".

---

## 9. What the Analyst is asked

The Analyst (the single LLM component) receives aggregates only and is asked, in order:

1. What work did we not get, ranked by volume?
2. For each cause — is this work we want?
3. If yes, what specifically would it take: which truck class, which hours, which territory?
4. If no, which client should be asked to stop sending it, and what is the volume?
5. What changed versus last period?

Every claim must carry its supporting number; unsourced statements are dropped before
output. Proposals go to `config/rules.proposed.yaml` for human review — the Analyst never
writes `rules.yaml`.
