"""The Analyst -- the ONLY component in this system that uses an LLM.

Hard constraint #3. Acquisition, ingestion, classification, metrics and
notification are fully deterministic: the same input always produces the same
numbers, and nobody has to wonder whether a model was having an off day. The
Analyst adds a paragraph of prose and a queue of suggestions on top of numbers
that were already final before it ran.

Five rules keep that boundary honest:

1. **Aggregates only.** :func:`sanitize_metrics` strips anything row level
   before the request leaves this process. A per-request row would put pickup
   addresses and driver names into a third party prompt for no analytical gain,
   because the metrics object already answers every question the narrative asks.

2. **Every claim carries its number.** The prompt demands it and
   :func:`_keep_sourced` enforces it: any statement that arrives without a
   supporting number is dropped, not printed. An unsourced observation from a
   language model is a guess wearing a report's clothes.

3. **NO DOLLARS IN THE PROSE. EVER, AND UNCONDITIONALLY.** ``offerAmount`` is
   empty on 100% of the records this account produces -- 3,079 of 3,079 over 30
   days -- so nothing Towbook sends carries a price. The prompt says so in as
   many words, and :func:`_money_claim` enforces it: a statement mentioning
   revenue, dollars or job value is dropped and logged. This is not
   squeamishness. The owner acts on the biggest number in the report, and a
   hallucinated "$40,000 of missed work" is a number he cannot check and would
   be entirely right to believe.

   Note what this rule is **not**. It does not say the system may never show
   money. ``rules.yaml -> missed_work.job_value_by_client`` holds real
   per-client average job values the owner supplied from invoice data, and
   agents/missed_work.py multiplies them out into ``estimated_value``
   deterministically -- a traceable number, reproducible from a table anyone can
   read. That number is fine, and it is not this module's to produce.

   The guard is on the *language model's prose*, and it does not lift when the
   price book is populated. A figure a model wrote in a sentence is unverifiable
   whether or not average values exist: it can apply a tow average to a missed
   tire change, quote the ``urgent.ly`` value that config flags as an assumption
   without flagging it, or simply multiply wrong -- and every one of those
   arrives looking exactly like the traceable number. So the Analyst counts
   jobs, and every dollar in this system comes from the deterministic layer.

4. **The questions are the missed-work questions.** MISSED_WORK_MODEL.md
   section 9, in order: what did we not get ranked by volume; for each cause is
   this work we want; if yes what specifically would it take; if no which client
   should be asked to stop and what is the volume; what changed versus last
   period. Acceptance rate is context for those answers, not the subject.

   The five questions are the same at every cadence. What the answers are FOR
   is not, and :data:`_CADENCE_BRIEFS` says so in the prompt: the daily is
   operational (what did we lose yesterday, what needs doing today), the weekly
   is a ranked list of improvement recommendations each carrying the number
   that justifies it, and the monthly is trend and strategy (what is getting
   better or worse, what to do next month). Without that, one prompt produces
   the same paragraph three times a month.

5. **The Analyst never writes config/rules.yaml.** Proposals go to
   config/rules.proposed.yaml and sit there until a human copies them across.
   See :func:`_proposals_path` for why this is enforced in code rather than by
   convention.

If ANTHROPIC_API_KEY is unset -- or the API errors, or returns something
unparseable -- the Analyst degrades to a deterministic narrative that leads
with the same missed-work inventory and reports ``{"llm": "unavailable"}``. The
pipeline never fails because the commentary did.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..core.config_loader import CONFIG, config_path, rules_version
from ..core.logging_setup import get_logger

__all__ = [
    "analyze",
    "sanitize_metrics",
    "compact_for_prompt",
    "record_proposals",
    "parse_json_response",
    "AnalystError",
    "DEFAULT_MODEL",
    "ROW_LEVEL_KEYS",
    "ROW_LEVEL_FIELDS",
    "PROPOSALS_CONFIG_NAME",
    "PROPOSALS_FILENAME",
    "MONEY_PATTERN",
]

logger = get_logger(__name__)

#: Overridden by ANTHROPIC_MODEL. Kept as a plain alias with no date suffix.
DEFAULT_MODEL: str = "claude-sonnet-5"

#: Default output ceiling. Generous because current models think adaptively and
#: thinking tokens share this budget with the JSON body; a tight cap truncates
#: the JSON mid-object and turns a good report into a parse error.
DEFAULT_MAX_TOKENS: int = 16000

#: On truncation the request is retried at double the budget, up to this many
#: times and never past MAX_TOKENS_CEILING. Truncation is a budget problem, not
#: a parse problem, and dropping a monthly analysis to the boilerplate template
#: because the JSON stopped mid-object is the worst available outcome.
MAX_TOKEN_ESCALATIONS: int = 2
MAX_TOKENS_CEILING: int = 64000

#: Hard ceiling on the serialised metrics we are willing to send. A metrics
#: object far bigger than this is a bug upstream (someone attached rows), not a
#: reason to ship a 2 MB prompt.
#:
#: Raised from 60,000 when the missed-work model landed: a weekly document now
#: carries the inventory, the 7 x 24 blind-spot analysis, the close-off
#: candidates and the client comparison, and measured at 88,000 chars on real
#: traffic. Truncation is not a graceful degradation here -- it cuts the JSON
#: mid-object and hands the model a document that is both incomplete and
#: invalid, with no signal about which half is missing. See
#: :func:`compact_for_prompt`, which does the honest version of the same job.
MAX_PROMPT_CHARS: int = 120_000

#: The only config file this module may write.
PROPOSALS_CONFIG_NAME: str = "rules_proposed"
PROPOSALS_FILENAME: str = "rules.proposed.yaml"


class AnalystError(RuntimeError):
    """Something went wrong inside the Analyst. Never escapes :func:`analyze`."""


# ==========================================================================
# Guard rail: aggregates only
# ==========================================================================

#: Keys that, by name, hold per-request rows. Dropped outright.
ROW_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "requests",
        "request_rows",
        "rows",
        "raw_rows",
        "raw",
        "records",
        "sample_requests",
        "sample_rows",
        "request_ids",
        "detail_rows",
        "line_items",
        # The missed-work document's per-job lookup table (missed_work.py ->
        # _job_list_from). Every row is one offer, named by the reference the
        # owner searches Towbook with. It is dropped by name as well as by
        # shape: the shape test would catch it today, and naming it means a
        # future row that happens to carry only one identity field cannot
        # quietly start reaching the model.
        "missed_jobs",
    }
)

#: Fields from the canonical request record that identify one job. A mapping
#: carrying request_id -- or two or more of these -- is a row, not a total.
#:
#: service_type_raw is deliberately ABSENT: it names a *type* of work, not a
#: job, and the Analyst needs those verbatim strings to propose a
#: classification rule. That is the whole point of hard constraint #6.
ROW_LEVEL_FIELDS: frozenset[str] = frozenset(
    {
        "request_id",
        "pickup_location",
        "dropoff_location",
        "driver_assigned",
        "truck_assigned",
        "offered_at",
        "responded_at",
        "ingested_at",
        "source_run_id",
        "call_number",
        # The Towbook job number and the reference derived from it. Both name
        # ONE job, so a mapping carrying either alongside any other identity
        # field is a row and never reaches the model.
        "job_number",
        "towbook_ref",
        "towbook_ref_label",
        "po",
        "po#",
        "po_number",
        "vehicle",
        "amount",
    }
)

#: Longest list we will send for any single key.
_MAX_LIST_ITEMS: int = 40
#: Longest string we will send for any single value.
_MAX_STRING_CHARS: int = 400


def _looks_row_level(value: Any) -> bool:
    """True when ``value`` is a mapping describing one individual request."""
    if not isinstance(value, dict):
        return False
    keys = {str(key).strip().lower() for key in value}
    if "request_id" in keys:
        return True
    # Two or more identity fields together is a row. One on its own may be a
    # legitimate aggregate ("amount" as a daily revenue total, for instance).
    return len(keys & ROW_LEVEL_FIELDS) >= 2


def sanitize_metrics(metrics: Any, _removed: list[str] | None = None, _path: str = "") -> tuple[Any, list[str]]:
    """Return ``(aggregates_only_copy, removed_paths)``.

    The guard from the build spec: if anything row level is passed, strip it
    before the API call. Also bounds list length and string size so a runaway
    aggregate cannot turn into a runaway prompt.
    """
    removed = _removed if _removed is not None else []

    if isinstance(metrics, dict):
        clean: dict[str, Any] = {}
        for key, value in metrics.items():
            key_text = str(key)
            path = f"{_path}.{key_text}" if _path else key_text

            if key_text.strip().lower() in ROW_LEVEL_KEYS:
                removed.append(path)
                continue
            if _looks_row_level(value):
                removed.append(path)
                continue
            if isinstance(value, list) and any(_looks_row_level(item) for item in value):
                removed.append(path)
                continue

            cleaned, _ = sanitize_metrics(value, removed, path)
            clean[key_text] = cleaned
        return clean, removed

    if isinstance(metrics, (list, tuple)):
        items = list(metrics)
        truncated = items[:_MAX_LIST_ITEMS]
        if len(items) > _MAX_LIST_ITEMS:
            removed.append(f"{_path}[{_MAX_LIST_ITEMS}:] ({len(items) - _MAX_LIST_ITEMS} items trimmed)")
        return [sanitize_metrics(item, removed, _path)[0] for item in truncated], removed

    if isinstance(metrics, str) and len(metrics) > _MAX_STRING_CHARS:
        return metrics[:_MAX_STRING_CHARS] + "...", removed

    return metrics, removed


#: Dense arrays inside the missed-work document that carry no information the
#: prompt can use. The 7 x 24 grids are 168 numbers each, four of them, plus a
#: 168-entry `cells` list -- roughly 20 KB of JSON restating what
#: `blind_spots.blind_spots` and `blind_spots.by_hour` already say in a form a
#: reader can act on. Dropping them is not lossy for the questions being asked,
#: and it is what keeps a weekly prompt (which carries this week AND last week)
#: inside MAX_PROMPT_CHARS instead of being truncated mid-JSON.
_DENSE_BLIND_SPOT_KEYS: frozenset[str] = frozenset(
    {"offers", "accepted", "no_response", "no_response_rate", "cells"}
)


#: Dropped from the prompt entirely on the weekly path.
#:
#: ``compute_weekly`` embeds the whole prior-week missed-work document so the
#: report can compute a trend. It already did: ``missed_work_trend`` carries the
#: per-cause deltas, the totals with direction, which blind spots opened and
#: closed, and which close-off candidates resolved -- which is the entirety of
#: what question 5 asks. Shipping the source document as well costs ~12,000
#: characters to restate an answer the input already contains.
_REDUNDANT_PROMPT_KEYS: frozenset[str] = frozenset({"prior_missed_work"})


def compact_for_prompt(metrics: Any) -> Any:
    """Trim the prompt copy to what the five questions actually refer to.

    Two things go: the dense 7 x 24 grids inside any missed-work document, and
    the prior-week document that ``missed_work_trend`` already summarises.

    Runs after :func:`sanitize_metrics`, never instead of it: this is about
    prompt size, that one is about not sending row-level data. Everything a
    question in the prompt refers to -- totals, by_cause, inventory, the flagged
    blind spots, by_hour, closeoff candidates, client comparison, and the whole
    of missed_work_trend -- survives untouched.

    Measured on a real weekly document: 88,000 chars sanitised, 48,000 after
    this. The alternative is truncation, which cuts the JSON mid-object.
    """
    if isinstance(metrics, list):
        return [compact_for_prompt(item) for item in metrics]
    if not isinstance(metrics, dict):
        return metrics

    compact: dict[str, Any] = {}
    for key, value in metrics.items():
        if key in _REDUNDANT_PROMPT_KEYS:
            continue
        # Gated on the dense keys themselves, NOT on the grid's `rows`
        # dimension: `rows` is in ROW_LEVEL_KEYS, so sanitize_metrics has
        # already deleted it by the time this runs. Two different meanings of
        # the same word, and testing for the wrong one made this a no-op.
        if (
            key == "blind_spots"
            and isinstance(value, dict)
            and _DENSE_BLIND_SPOT_KEYS & value.keys()
        ):
            trimmed = {
                inner: item
                for inner, item in value.items()
                if inner not in _DENSE_BLIND_SPOT_KEYS
            }
            trimmed["grid_omitted"] = (
                "The full 7 x 24 grid is in the report, not in this prompt. "
                "`blind_spots` lists the flagged cells and `by_hour` the "
                "hour-of-day totals."
            )
            compact[key] = trimmed
            continue
        compact[key] = compact_for_prompt(value)
    return compact


def _serialise(value: Any) -> str:
    """JSON for the prompt. ``default=str`` so dates and Decimals survive."""
    text = json.dumps(value, indent=2, sort_keys=True, default=str, ensure_ascii=False)
    if len(text) > MAX_PROMPT_CHARS:
        # ERROR, not warning. This does not shorten the document, it BREAKS it:
        # the cut lands mid-object and the model is handed invalid JSON with no
        # indication of which half went missing. The fix is to teach
        # compact_for_prompt about whatever grew, not to raise the ceiling
        # again and hope.
        logger.error(
            "metrics object is %d chars, over the %d limit; the prompt JSON will "
            "be cut mid-object. Add the oversized section to compact_for_prompt.",
            len(text),
            MAX_PROMPT_CHARS,
        )
        text = text[:MAX_PROMPT_CHARS] + "\n... (TRUNCATED -- this JSON is incomplete)"
    return text


# ==========================================================================
# Prompting
# ==========================================================================

_SYSTEM_PROMPT = """\
You are the analyst for a towing company's missed-work monitoring system.

The owner's question is not "what are we accepting". It is:

    What are we NOT accepting? Do we want it? If yes, what do we need to do to
    accept it? If no, how do we stop it being offered at all?

Acceptance rate is a SYMPTOM. Your deliverable is an inventory of missed work,
attributed to a cause, with an action attached. Lead with what was missed.
Report acceptance rate only as supporting context.

You are given AGGREGATE metrics only -- counts, rates and per-client or
per-service-class totals for one reporting window. You never see individual
jobs, and you must not ask for them or invent them.

Write in the plain language a dispatcher would use. Write like someone who
knows the business, not like a dashboard.

Non-negotiable rules:

1. YOU DO NOT WRITE DOLLAR FIGURES. Towbook sends this account an empty offer
   amount on 100% of records -- 3,079 of 3,079 over thirty days -- so nothing
   in this feed carries a price. You must not estimate, infer, imply or gesture
   at revenue, dollars, lost income, job value, "worth", margin or "cost us".
   Not with a figure, not with a range, not with a hedge like "likely
   thousands". Count JOBS. If you catch yourself reaching for money, say the
   job count instead.

   This holds even if the data you are given contains an `estimated_value`.
   That figure is computed elsewhere, from a table of average job values a
   human entered, and the report shows it on its own. It is not yours to
   quote, restate or multiply. Any statement of yours that mentions money is
   deleted before the owner sees it, so it is wasted output.
2. EVERY statement must carry the specific number that supports it in its own
   `supporting_number` field. If you cannot point at a number in the data
   provided, do not make the statement. No estimates, no "roughly", no claims
   about causes you cannot see in the data.
3. Never state a number that is not in the input, and never compute a
   comparison against a period that is not in the input.
4. Every ranking you produce is BY JOB COUNT, and you say so.
5. Say "no notable change" rather than manufacturing a finding. A quiet week is
   a legitimate result.
6. Rule proposals are SUGGESTIONS for a human to review. They are written to a
   separate file and never applied automatically. Only propose a rule when the
   data actually shows a gap -- most commonly a service type string that keeps
   landing in the `unclassified` bucket. Quote the verbatim service type
   strings that motivated it.
7. Output STRICT JSON matching the requested schema. No prose outside the JSON,
   no markdown code fences, no trailing commentary.
"""

#: What each cadence is FOR, substituted into the user prompt at
#: ``{cadence_brief}``.
#:
#: The five questions below are the same for every report -- they are the
#: missed-work model (MISSED_WORK_MODEL.md section 9) and they do not change
#: with the clock. What changes is what the reader is about to DO with the
#: answers, and a model that does not know that writes the same paragraph three
#: times a month:
#:
#:   DAILY   is read at 06:00 with a phone in one hand. It is operational.
#:           Yesterday's losses, today's actions, no strategy, no adjectives.
#:   WEEKLY  is read when deciding what to change. It is a ranked list of
#:           recommendations, each one justified by the number that motivates it.
#:   MONTHLY is read when deciding whether the change worked, and what to fund
#:           next. It is trend and direction, and a level with no direction has
#:           no place in it.
#:
#: One dict, so a fourth cadence is an entry here rather than a second prompt.
_CADENCE_BRIEFS: dict[str, str] = {
    "hourly": """\
THIS IS THE HOURLY REPORT. One hour of traffic, read within minutes of the hour
closing. Say what was offered and what went unanswered, in as few words as
possible. Do not draw trends from one hour -- an hour is noise. If nothing
needs a response, say so in one sentence and stop.""",
    "daily": """\
THIS IS THE DAILY REPORT. It is read first thing in the morning, on a phone,
before the day starts. It is OPERATIONAL, not strategic.

Answer two things and nothing else:
  1. WHAT DID WE LOSE YESTERDAY -- the count, the biggest cause, and where it
     landed.
  2. WHAT NEEDS ACTION TODAY -- something a dispatcher or the owner can act on
     before this evening. Naming an hour to cover, a client to call, or a truck
     to move counts. "Improve response times" does not.

Be TERSE. Short sentences. No strategy, no month-scale framing, no
recommendations that take a week to implement. Do not speculate about causes
you cannot see in one day of data -- one day is a small sample and a confident
story built on it is worse than no story. If yesterday was ordinary, say it was
ordinary in one sentence.""",
    "weekly": """\
THIS IS THE WEEKLY REPORT. It is read when deciding WHAT TO CHANGE. Its
deliverable is a RANKED LIST OF IMPROVEMENT RECOMMENDATIONS, and every single
one carries the number that justifies it.

Rank by the number of jobs the recommendation would recover, biggest first. For
each one, say plainly:
  - what to change (a specific, nameable action -- staff 17:00-21:00, ask this
    client to stop sending this service type, add this truck class),
  - how many jobs that is worth, from the data in front of you,
  - and how you would know next week whether it worked.

A recommendation without a number attached is an opinion, and it will be
deleted before the owner reads it. If the week supports only two
recommendations, give two. Do not pad the list.

You may compare against the prior week where the input contains one, but the
report is about the DECISION, not the history.""",
    "monthly": """\
THIS IS THE MONTHLY REPORT. It is read when deciding whether last month's
changes worked and what to fund next. Its subject is TREND AND STRATEGY.

A level with no direction does not belong in this report. Every claim you make
compares this month against last month, and carries both numbers.

Answer, in this order:
  1. WHAT IS GETTING BETTER -- which causes shrank, which blind spots closed,
     which close-off requests actually stopped the offers coming. Both numbers.
  2. WHAT IS GETTING WORSE -- which causes grew, which hours deteriorated,
     which clients are on a declining trajectory. Both numbers.
  3. WHAT TO DO NEXT MONTH -- one to three moves, in priority order, each one
     justified by a trend rather than by a single month's level.

Two things that matter at this horizon and at no shorter one:

  * COVERAGE IS THE FINDING THIS SYSTEM EXISTS TO SURFACE. Response is close to
    flawless in the hours somebody is on it and collapses outside them, on the
    same trucks and the same staff. If the coverage data supports it, say which
    hours, with the unanswered rate inside and outside them, and whether the
    gap narrowed or widened against last month.
  * MONTHS ARE DIFFERENT LENGTHS. A 31-day month against a 30-day month is a 3%
    volume difference before anything real happened. Where the input gives you a
    per-day figure, use it for volume comparisons and say that is what you are
    doing.

Take the longer view: a swing that would be worth reporting in a day is usually
noise in a month. Say "no notable change" rather than manufacture a trend.""",
}

#: Used for a report type with no entry above, and for ad-hoc calls.
_DEFAULT_CADENCE_BRIEF = """\
This is an ad-hoc report over one window. Answer the five questions from the
data given, and do not compare against a period that is not in the input."""

#: The `narrative` field, per cadence. Every one of them still LEADS WITH WHAT
#: WE MISSED -- that is the model, not a stylistic preference -- but what
#: follows the first sentence is what the reader is about to do.
_NARRATIVE_BRIEFS: dict[str, str] = {
    "hourly": """1-2 sentences. What was offered, what went
  unanswered, whether anything needs a response now. Nothing else.""",
    "daily": """2-4 SHORT sentences for the owner, read on a phone at
  06:00. LEAD WITH WHAT WE LOST YESTERDAY and how much of it was recoverable,
  then the single biggest cause, then the one thing to do about it TODAY.
  Operational and specific. No strategy, no month-scale claims, no adjectives.
  Acceptance rate belongs in the last sentence, if at all.""",
    "weekly": """3-5 sentences. LEAD WITH WHAT WE MISSED and how much
  of it was recoverable, then go straight to the TOP RECOMMENDATION and the
  number of jobs it would recover. The reader is deciding what to change this
  week, so the narrative ends on a decision, not on a description. Acceptance
  rate belongs in the last sentence, if at all.""",
    "monthly": """3-6 sentences, written for someone deciding what to
  fund next month. LEAD WITH THE DIRECTION -- missed work this month against
  last, both numbers -- then what improved, then what deteriorated, then the
  single most important move for next month and the trend that justifies it.
  Every claim carries both months' numbers. Do not restate a level without its
  direction; that is the weekly's job. Acceptance rate belongs last, if at
  all.""",
}

_DEFAULT_NARRATIVE_BRIEF = """2-5 sentences for the owner. LEAD WITH WHAT
  WE MISSED and how much of it was recoverable, then the single biggest cause
  and the action it implies. Acceptance rate belongs in the last sentence, if
  at all."""


def _cadence_brief(report_type: str) -> str:
    return _CADENCE_BRIEFS.get(str(report_type), _DEFAULT_CADENCE_BRIEF)


def _narrative_brief(report_type: str) -> str:
    return _NARRATIVE_BRIEFS.get(str(report_type), _DEFAULT_NARRATIVE_BRIEF)


_USER_TEMPLATE = """\
Reporting window: {report_type}
Rules version that produced these numbers: {rules_version}

{cadence_brief}

Service classes currently defined in the rules (anything not matching these
falls through to `unclassified`):
{service_classes}

Work the owner says he WANTS (acceptance_policy.should_accept):
{should_accept}

How to read the numbers:
{money_rule}

Context you will need: the median response window Towbook allows is 3 minutes
(mean 4, max 15). An offer nobody answers is lost almost immediately, so an
unanswered offer is a coverage problem, never a pricing one.

Aggregate metrics for this window. `missed_work` is the headline: `totals`
holds the bucket split, `by_cause` the attribution, `inventory` the ranked
(service class, cause) pairs, `coverage` the split by whether the offer arrived
inside the owner's staffed window, `blind_spots` the hour-of-week windows where
offers go unanswered, `closeoff_candidates` the work that is refused nearly
every time it is offered, `client_comparison` the gap between clients that
should behave alike.

`coverage` is the cut that matters most and the one to lead with when it is
present. Raw hour-of-day is a weaker version of the same fact. Quote the
outside-hours figure AND the inside-hours figure together -- one without the
other is an assertion rather than evidence -- and remember that the trucks are
identical in both, so an unanswered offer outside the window is a staffing
answer, never an equipment one.
```json
{metrics}
```

Answer these five questions, in this order, as JSON with exactly these keys:

1. "missed_work_ranked" -- WHAT DID WE NOT GET, ranked by volume? Array of
   {{"work": ..., "missed": ..., "cause": ..., "supporting_number": ...}}.
   `work` is the service class or service type. `missed` is the job count as an
   integer. Ranked by job count, biggest first. Empty array if nothing was
   missed.

2. + 3. "cause_assessments" -- FOR EACH CAUSE, IS THIS WORK WE WANT, and if yes
   WHAT SPECIFICALLY WOULD IT TAKE? Array of
   {{"cause": ..., "want_this_work": "yes"|"no"|"unclear",
     "what_it_would_take": ..., "supporting_number": ...}}.
   `what_it_would_take` must be concrete and drawn from the data: WHICH TRUCK
   CLASS for an equipment cause, WHICH HOURS for a staffing or attention cause
   (name them from blind_spots), WHICH TERRITORY for a coverage cause. "Improve
   coverage" is not an answer; "cover 17:00-21:00, where 47% of offers go
   unanswered" is.

4. "close_off_requests" -- IF WE DO NOT WANT IT, WHICH CLIENT SHOULD BE ASKED
   TO STOP SENDING IT, AND WHAT IS THE VOLUME? Array of
   {{"client": ..., "service_type": ..., "supporting_number": ...}}.
   Draw these from closeoff_candidates. Say the volume in jobs. Note in the
   narrative that closing these off is expected to improve the response rate on
   work we do want, because it removes competing offers from the same 3-minute
   decision window.

5. "trend_statements" -- WHAT CHANGED VERSUS LAST PERIOD? Array of
   {{"statement": ..., "supporting_number": ...}}. Only where the input
   actually contains a prior period. `supporting_number` must be the concrete
   figure, e.g. "62 equipment declines against 41 last week". A statement
   without one will be discarded.

Plus these three:

- "narrative": {narrative_brief}
- "clients_needing_attention": array of objects, each
  {{"client": ..., "reason": ..., "supporting_numbers": ...}}.
  Only clients the data actually flags -- especially a client whose offers go
  unanswered far more often than another client's, which is a process
  difference, not a capacity one. Empty array if none.
- "proposed_rules": array of objects, each
  {{"id": ..., "target": ..., "rationale": ..., "service_type_raw_samples": [...],
    "patch_yaml": ..., "occurrence_count": ...}}.
  `target` is one of service_classes, acceptance_policy, alerts,
  denial_reason_normalization. `patch_yaml` is the YAML fragment a human would
  paste into config/rules.yaml, as a string. `id` is a short stable kebab-case
  slug. Empty array if the rules already cover what you see.
"""

#: Substituted into the prompt at ``{money_rule}``.
#:
#: ONE version, deliberately. An earlier draft had a second, softer wording for
#: when missed_work.job_value_by_client is populated -- and the owner populated
#: it on 2026-07-28, which would have silently switched the rule off in the
#: middle of this build. The existence of average job values changes what the
#: *report* may display; it does not make a sentence a model wrote any more
#: checkable. See the module docstring, rule 3.
_NO_MONEY_RULE = """\
  Every figure you produce is a JOB COUNT. Towbook returns an empty offer
  amount on 100% of this account's records, so nothing in this feed carries a
  price and no dollar figure can be derived from it.

  If the data below contains an `estimated_value`, it was computed from a table
  of average job values a human entered by hand. The report shows that figure
  itself. It is not yours to quote, restate or multiply, and a dollar amount in
  your output will be deleted whether or not that table exists. Rank by job
  count and say that is what you are doing."""

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "narrative": {"type": "string"},
        # -- MISSED_WORK_MODEL.md section 9, questions 1-4 -------------------
        # `supporting_number` is required on every one of these, which is what
        # lets _keep_sourced() drop an unsourced claim rather than print it.
        "missed_work_ranked": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "work": {"type": "string"},
                    "missed": {"type": "integer"},
                    "cause": {"type": "string"},
                    "supporting_number": {"type": "string"},
                },
                "required": ["work", "missed", "cause", "supporting_number"],
                "additionalProperties": False,
            },
        },
        "cause_assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cause": {"type": "string"},
                    "want_this_work": {"type": "string", "enum": ["yes", "no", "unclear"]},
                    "what_it_would_take": {"type": "string"},
                    "supporting_number": {"type": "string"},
                },
                "required": [
                    "cause",
                    "want_this_work",
                    "what_it_would_take",
                    "supporting_number",
                ],
                "additionalProperties": False,
            },
        },
        "close_off_requests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "client": {"type": "string"},
                    "service_type": {"type": "string"},
                    "supporting_number": {"type": "string"},
                },
                "required": ["client", "service_type", "supporting_number"],
                "additionalProperties": False,
            },
        },
        "clients_needing_attention": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "client": {"type": "string"},
                    "reason": {"type": "string"},
                    "supporting_numbers": {"type": "string"},
                },
                "required": ["client", "reason", "supporting_numbers"],
                "additionalProperties": False,
            },
        },
        "trend_statements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "statement": {"type": "string"},
                    "supporting_number": {"type": "string"},
                },
                "required": ["statement", "supporting_number"],
                "additionalProperties": False,
            },
        },
        "proposed_rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "target": {
                        "type": "string",
                        "enum": [
                            "service_classes",
                            "acceptance_policy",
                            "alerts",
                            "denial_reason_normalization",
                        ],
                    },
                    "rationale": {"type": "string"},
                    "service_type_raw_samples": {"type": "array", "items": {"type": "string"}},
                    "patch_yaml": {"type": "string"},
                    "occurrence_count": {"type": "integer"},
                },
                "required": [
                    "id",
                    "target",
                    "rationale",
                    "service_type_raw_samples",
                    "patch_yaml",
                    "occurrence_count",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "narrative",
        "missed_work_ranked",
        "cause_assessments",
        "close_off_requests",
        "clients_needing_attention",
        "trend_statements",
        "proposed_rules",
    ],
    "additionalProperties": False,
}


def _service_class_summary() -> str:
    try:
        rules = CONFIG.get_rules()
    except Exception:  # pragma: no cover - config unreadable
        return "  (rules unavailable)"
    classes = rules.get("service_classes") or {}
    lines = []
    for name, spec in classes.items():
        if str(name).startswith("_"):
            continue
        matches = (spec or {}).get("match_any") if isinstance(spec, dict) else None
        lines.append(f"  - {name}: matches {', '.join(str(m) for m in (matches or []))}")
    return "\n".join(lines) or "  (none defined)"


def _should_accept_summary() -> str:
    """The classes the owner says he wants, so "do we want it" has an anchor."""
    try:
        rules = CONFIG.get_rules()
    except Exception:  # pragma: no cover - config unreadable
        return "  (rules unavailable)"
    policy = rules.get("acceptance_policy")
    wanted = (policy or {}).get("should_accept") if isinstance(policy, dict) else None
    if not wanted:
        return "  (not configured -- treat every class as potentially wanted)"
    return "  " + ", ".join(str(name) for name in wanted)


# ==========================================================================
# The money guard
# ==========================================================================

#: A claim about money. Deliberately broad, because the failure it prevents is
#: expensive and a false positive costs one dropped sentence.
#:
#: offerAmount is empty on 100% of this account's records, so a dollar figure in
#: a report can only have been invented. The owner will act on the biggest
#: number he is shown, and an invented one is a number he cannot check and would
#: be entirely reasonable to believe.
MONEY_PATTERN: re.Pattern[str] = re.compile(
    r"""
    [$£€]\s*\d          # $1,200 / $ 40k
    | \b\d[\d,.]*\s*(?:k|m)?\s*(?:dollars|usd)\b
    | \b(?:revenue|dollars?|usd)\b
    | \b(?:lost|missed|potential|estimated|forecast)\s+(?:income|earnings|billings|value)\b
    | \bjob\s+value\b
    | \bworth\s+(?:about\s+|around\s+|roughly\s+)?[$\d]
    | \bmargin\b
    | \bcost\s+(?:us|the\s+business|you)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _money_claim(*parts: Any) -> bool:
    """True when any part reads as a claim about money."""
    return any(MONEY_PATTERN.search(str(part or "")) for part in parts)


def _revenue_available(metrics: Any) -> bool:
    """Whether a human supplied real per-client average job values.

    Reported on the result so a consumer knows whether the deterministic layer
    could price anything. It deliberately does NOT gate :func:`_money_claim`:
    average values make the *report's* computed figure meaningful, they do not
    make a sentence the model wrote checkable. See the module docstring, rule 3.

    Read from the missed-work document rather than from rules.yaml, so it
    describes the data actually in this report rather than the config as it
    stands now.
    """
    if not isinstance(metrics, dict):
        return False
    if metrics.get("revenue_available"):
        return True
    document = metrics.get("missed_work")
    return bool(isinstance(document, dict) and document.get("revenue_available"))


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _strip_money_sentences(narrative: str) -> tuple[str, int]:
    """Remove sentences that talk about money. Returns ``(text, dropped)``.

    Sentence level rather than all-or-nothing: one stray "which cost us
    thousands" should not delete a paragraph of correct analysis, and rewriting
    the model's prose would be worse than deleting the offending claim.
    """
    text = str(narrative or "").strip()
    if not text or not MONEY_PATTERN.search(text):
        return text, 0
    kept = [part for part in _SENTENCE_SPLIT.split(text) if not MONEY_PATTERN.search(part)]
    dropped = len(_SENTENCE_SPLIT.split(text)) - len(kept)
    return " ".join(part.strip() for part in kept).strip(), dropped


# ==========================================================================
# Defensive JSON parsing
# ==========================================================================

_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")


def parse_json_response(text: str) -> dict:
    """Parse the model's reply into a dict, tolerating code fences and preamble.

    Structured outputs make this unnecessary on a good day. It exists for the
    bad day -- an older ANTHROPIC_MODEL that does not support the schema
    parameter, or a model that decided to be helpful and wrap the JSON.
    """
    if not text or not text.strip():
        raise AnalystError("model returned an empty response")

    candidate = text.strip()
    candidate = _FENCE.sub("", candidate).strip()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            raise AnalystError("model response contained no JSON object") from None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AnalystError(f"model response was not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise AnalystError(f"model returned {type(parsed).__name__}, expected a JSON object")
    return parsed


#: Values a model offers when it has no number and would rather not say so.
_EMPTY_NUMBERS: frozenset[str] = frozenset(
    {"n/a", "na", "none", "null", "-", "--", "?", "tbd", "unknown", "not available"}
)


def _keep_sourced(
    items: Any,
    fields: tuple[str, ...],
    *,
    label: str,
    number_field: str = "supporting_number",
) -> tuple[list[dict], int]:
    """Keep only entries that carry a real supporting number and no money claim.

    One filter for all five answer lists, because the rule is the same for all
    five: an instruction is a request, and a report that quietly prints
    unsourced claims is worse than one that prints fewer of them.

    Two ways to be dropped:

    * **no number.** ``supporting_number`` missing, blank, or one of the polite
      evasions in :data:`_EMPTY_NUMBERS`.
    * **money.** Any field mentioning revenue, dollars or job value. There is no
      opt-out parameter here on purpose: see the module docstring, rule 3. The
      deterministic layer owns every dollar figure this system shows.

    ``fields`` names the text fields to copy through; the first is used for the
    log line, so put the most identifying one first.
    """
    kept: list[dict] = []
    dropped = 0
    for item in items or []:
        if not isinstance(item, dict):
            dropped += 1
            continue

        entry: dict[str, Any] = {}
        for field in fields:
            value = item.get(field)
            entry[field] = value if isinstance(value, int) and not isinstance(value, bool) else str(
                value or ""
            ).strip()

        headline = str(entry.get(fields[0]) or "").strip()
        if not headline:
            dropped += 1
            continue

        number = item.get(number_field)
        number_text = "" if number is None else str(number).strip()
        if not number_text or number_text.lower() in _EMPTY_NUMBERS:
            logger.warning("dropped unsourced %s: %s", label, headline[:160])
            dropped += 1
            continue

        if _money_claim(*entry.values(), number_text):
            logger.warning(
                "dropped %s claiming a dollar value (offer amounts are empty on "
                "100%% of records, so it cannot be sourced): %s",
                label,
                headline[:160],
            )
            dropped += 1
            continue

        entry[number_field] = number_text
        kept.append(entry)
    return kept, dropped


#: ``(result key, text fields, log label, number field)`` for every list the
#: model returns. Each one goes through :func:`_keep_sourced` on the same terms:
#: no supporting number, no entry; a dollar figure with no source, no entry.
_SOURCED_LISTS: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    ("missed_work_ranked", ("work", "missed", "cause"), "missed-work row", "supporting_number"),
    (
        "cause_assessments",
        ("cause", "want_this_work", "what_it_would_take"),
        "cause assessment",
        "supporting_number",
    ),
    (
        "close_off_requests",
        ("client", "service_type"),
        "close-off request",
        "supporting_number",
    ),
    (
        "clients_needing_attention",
        ("client", "reason"),
        "client finding",
        "supporting_numbers",
    ),
    ("trend_statements", ("statement",), "trend statement", "supporting_number"),
)


# ==========================================================================
# The model call
# ==========================================================================


def _model_name() -> str:
    return (os.environ.get("ANTHROPIC_MODEL") or "").strip() or DEFAULT_MODEL


def _max_tokens() -> int:
    raw = (os.environ.get("ANTHROPIC_MAX_TOKENS") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_MAX_TOKENS


def _api_key_present() -> bool:
    return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())


def _response_text(response: Any) -> str:
    parts = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def _call_model(system_prompt: str, user_prompt: str) -> tuple[dict, str]:
    """Call the Messages API and return ``(parsed_json, model_id)``.

    Tries progressively simpler request shapes so that pointing
    ANTHROPIC_MODEL at an older model degrades instead of 400-ing:
    structured output + effort, then structured output, then plain JSON in the
    prompt. Note there is deliberately no ``temperature`` -- current models
    reject sampling parameters outright.
    """
    import anthropic  # local import: the Analyst is the only user of the SDK

    client = anthropic.Anthropic()
    model = _model_name()
    base: dict[str, Any] = {
        "model": model,
        "max_tokens": _max_tokens(),
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    variants: list[dict[str, Any]] = [
        {"output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA}}},
        {"output_config": {"format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA}}},
        {},
    ]

    last_error: Exception | None = None
    for index, extra in enumerate(variants):
        try:
            response = client.messages.create(**base, **extra)
        except anthropic.BadRequestError as exc:
            # An unsupported parameter for this model: fall down a rung.
            last_error = exc
            logger.warning(
                "analyst request variant %d rejected by %s (%s); trying a simpler shape",
                index,
                model,
                exc,
            )
            continue
        except anthropic.APIStatusError as exc:
            raise AnalystError(f"Anthropic API error {exc.status_code}: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise AnalystError(f"could not reach the Anthropic API: {exc}") from exc

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise AnalystError(
                f"model declined the request (category={getattr(details, 'category', None)})"
            )
        # Truncation is not a parse problem, it is a budget problem. Retrying
        # the SAME request shape with a bigger ceiling recovers the report;
        # falling straight through to parse_json_response would raise on the
        # half-written object and drop the whole analysis to the template.
        # Measured on a real monthly document: 8,000 tokens truncated the JSON
        # at character 6,137 because adaptive thinking shares this budget.
        attempts = 0
        while (
            getattr(response, "stop_reason", None) == "max_tokens"
            and attempts < MAX_TOKEN_ESCALATIONS
            and base["max_tokens"] < MAX_TOKENS_CEILING
        ):
            attempts += 1
            base["max_tokens"] = min(base["max_tokens"] * 2, MAX_TOKENS_CEILING)
            logger.warning(
                "analyst response hit max_tokens; retrying with max_tokens=%d (attempt %d/%d)",
                base["max_tokens"],
                attempts,
                MAX_TOKEN_ESCALATIONS,
            )
            try:
                response = client.messages.create(**base, **extra)
            except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
                logger.warning("analyst retry at a larger budget failed (%s)", exc)
                break

        if getattr(response, "stop_reason", None) == "max_tokens":
            logger.warning(
                "analyst response still truncated at max_tokens=%d; "
                "salvaging whatever parsed cleanly",
                base["max_tokens"],
            )

        return parse_json_response(_response_text(response)), model

    raise AnalystError(f"every request shape was rejected by {model}: {last_error}")


# ==========================================================================
# Proposals -- write-only to rules.proposed.yaml
# ==========================================================================


def _proposals_path() -> Path:
    """Return the ONE path this module is allowed to write.

    There is no ``path`` parameter anywhere in the proposal-writing code, and
    this function refuses to return anything that is not
    ``config/rules.proposed.yaml``.

    That is deliberate, and it is a safety property rather than an
    inconvenience. An LLM that can edit config/rules.yaml can silently change
    which jobs the business considers worth taking -- reclassify a service
    type, flip an acceptance policy, mute an alert -- and the owner would find
    out from the revenue, weeks later. Automated rule mutation is how a system
    drifts away from its owner's intent. Making the write helper structurally
    incapable of naming rules.yaml means a future refactor cannot reintroduce
    that path by accident.
    """
    path = config_path(PROPOSALS_CONFIG_NAME)
    if path.name != PROPOSALS_FILENAME:
        raise AnalystError(
            f"refusing to write {path.name}: the Analyst may only write {PROPOSALS_FILENAME}"
        )
    return path


def _leading_comment_block(path: Path) -> str:
    """Preserve the explanatory header at the top of rules.proposed.yaml.

    yaml.safe_dump throws comments away, and that header is what tells the next
    person reading the file that nothing in it has any effect until they act.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            lines.append(line)
            continue
        break
    while lines and not lines[-1].strip():
        lines.pop()
    return ("\n".join(lines) + "\n") if lines else ""


def _load_existing_proposals(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {"version": 1, "proposals": []}
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        logger.error("rules.proposed.yaml is not valid YAML (%s); starting a fresh list", exc)
        return {"version": 1, "proposals": []}
    if not isinstance(data, dict):
        return {"version": 1, "proposals": []}
    data.setdefault("version", 1)
    proposals = data.get("proposals")
    if not isinstance(proposals, list):
        data["proposals"] = []
    return data


def _slug(text: str, fallback: str = "proposal") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")
    return slug[:60] or fallback


def _normalise_proposal(raw: dict, report_type: str, now_iso: str) -> dict | None:
    """Shape one model-suggested proposal into the documented entry format."""
    if not isinstance(raw, dict):
        return None
    rationale = str(raw.get("rationale") or "").strip()
    if not rationale:
        return None

    target = str(raw.get("target") or "service_classes").strip()
    allowed = {"service_classes", "acceptance_policy", "alerts", "denial_reason_normalization"}
    if target not in allowed:
        logger.warning("ignoring proposal with unknown target %r", target)
        return None

    samples = [
        str(sample).strip()
        for sample in (raw.get("service_type_raw_samples") or [])
        if str(sample).strip()
    ][:20]

    patch_yaml = raw.get("patch_yaml")
    patch: Any = raw.get("patch")
    patch_error: str | None = None
    if patch is None and patch_yaml:
        try:
            patch = yaml.safe_load(str(patch_yaml))
        except yaml.YAMLError as exc:
            patch = None
            patch_error = f"proposed fragment is not valid YAML: {exc}"
            logger.warning("proposal %r carried unparseable YAML: %s", raw.get("id"), exc)
    if patch is not None and not isinstance(patch, (dict, list)):
        patch_error = "proposed fragment is not a YAML mapping"
        patch = None

    proposal_id = _slug(raw.get("id") or rationale[:40], fallback=f"{report_type}-proposal")
    occurrence = raw.get("occurrence_count")
    try:
        occurrence_count = max(0, int(occurrence))
    except (TypeError, ValueError):
        occurrence_count = len(samples)

    entry: dict[str, Any] = {
        "id": proposal_id,
        "proposed_at": now_iso,
        "source_report": report_type,
        "target": target,
        "rationale": rationale,
        # Hard constraint #6: the verbatim strings that motivated the proposal,
        # kept exactly as they arrived from the portal.
        "service_type_raw_samples": samples,
        "first_seen": now_iso,
        "last_seen": now_iso,
        "occurrence_count": occurrence_count,
        "evidence": {
            "sample_service_type_raw": samples,
            "affected_requests": occurrence_count,
        },
        "patch": patch,
        "patch_yaml": str(patch_yaml) if patch_yaml else None,
        "status": "pending",
        "reviewed_by": None,
        "reviewed_at": None,
        "rules_version": _safe_rules_version(),
    }
    if patch_error:
        entry["patch_error"] = patch_error
    return entry


def record_proposals(proposals: Iterable[dict], report_type: str = "manual") -> int:
    """Merge proposals into config/rules.proposed.yaml. Returns rows written.

    NOTE the absence of a path argument. See :func:`_proposals_path`.

    Merge semantics:

    * a proposal whose id a human has already accepted or rejected is left
      alone -- the Analyst does not get to reopen a decision;
    * a still-pending proposal with the same id has its occurrence_count and
      last_seen refreshed, and keeps its original first_seen;
    * anything new is appended with ``status: pending``.
    """
    proposals = [p for p in (proposals or []) if isinstance(p, dict)]
    if not proposals:
        return 0

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    normalised = [
        entry
        for entry in (_normalise_proposal(raw, report_type, now_iso) for raw in proposals)
        if entry is not None
    ]
    if not normalised:
        return 0

    try:
        path = _proposals_path()
    except AnalystError as exc:  # pragma: no cover - only reachable if config moves
        logger.error("%s", exc)
        return 0

    document = _load_existing_proposals(path)
    existing: list[dict] = [item for item in document["proposals"] if isinstance(item, dict)]
    by_id = {str(item.get("id")): item for item in existing if item.get("id")}

    written = 0
    for entry in normalised:
        current = by_id.get(entry["id"])
        if current is None:
            existing.append(entry)
            by_id[entry["id"]] = entry
            written += 1
            continue

        status = str(current.get("status") or "pending").lower()
        if status != "pending":
            logger.info(
                "proposal %s was already %s by a human; leaving it alone",
                entry["id"],
                status,
            )
            continue

        current["last_seen"] = entry["last_seen"]
        current["occurrence_count"] = max(
            int(current.get("occurrence_count") or 0), entry["occurrence_count"]
        )
        merged_samples = list(
            dict.fromkeys(list(current.get("service_type_raw_samples") or []) + entry["service_type_raw_samples"])
        )[:20]
        current["service_type_raw_samples"] = merged_samples
        current.setdefault("evidence", {})
        if isinstance(current["evidence"], dict):
            current["evidence"]["sample_service_type_raw"] = merged_samples
            current["evidence"]["affected_requests"] = current["occurrence_count"]
        current["rationale"] = entry["rationale"]
        if entry.get("patch") is not None:
            current["patch"] = entry["patch"]
        written += 1

    document["proposals"] = existing

    header = _leading_comment_block(path)
    body = yaml.safe_dump(document, sort_keys=False, allow_unicode=True, default_flow_style=False)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(header + body, encoding="utf-8")
        os.replace(temp, path)  # atomic: the dashboard never reads a half file
    except OSError as exc:
        logger.error("could not write %s: %s", path, exc)
        try:
            temp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass
        return 0

    CONFIG.reload(PROPOSALS_CONFIG_NAME)
    logger.info("recorded %d rule proposal(s) in %s for human review", written, path.name)
    return written


def _safe_rules_version() -> str:
    try:
        return rules_version()
    except Exception:  # pragma: no cover
        return "unknown"


# ==========================================================================
# Deterministic fallback
# ==========================================================================


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _pct_text(accepted: Any, offered: Any) -> str:
    a, o = _number(accepted), _number(offered)
    if a is None or o is None or o <= 0:
        return "0%"
    return f"{round(a / o * 100):.0f}%"


def _client_rows(metrics: dict) -> list[dict]:
    raw = metrics.get("clients") or metrics.get("by_client") or []
    if isinstance(raw, dict):
        rows = []
        for key, value in raw.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("client_key", key)
                rows.append(row)
        return rows
    return [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []


def _unclassified_samples(metrics: dict) -> list[tuple[str, int]]:
    """Verbatim service type strings that fell through to `unclassified`."""
    raw = (
        metrics.get("unclassified_service_types")
        or metrics.get("unclassified")
        or metrics.get("service_type_raw_counts")
        or []
    )
    pairs: list[tuple[str, int]] = []
    if isinstance(raw, dict):
        for text, count in raw.items():
            pairs.append((str(text), int(_number(count) or 0)))
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                text = item.get("service_type_raw") or item.get("value") or item.get("name")
                count = item.get("count") or item.get("occurrences") or item.get("offered")
                if text:
                    pairs.append((str(text), int(_number(count) or 0)))
            elif isinstance(item, str):
                pairs.append((item, 0))
    pairs.sort(key=lambda pair: (-pair[1], pair[0]))
    return pairs


def _missed_document(metrics: dict) -> dict:
    document = metrics.get("missed_work")
    return document if isinstance(document, dict) else {}


def _fallback_missed_work(metrics: dict) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """The missed-work answers, derived arithmetically from the document.

    Returns ``(ranked, cause_assessments, close_off_requests, sentences)``.

    Every one of these is a restatement of a number the deterministic pipeline
    already computed, which is exactly why the fallback can answer the same five
    questions the LLM is asked. What it cannot do is judge -- so
    ``want_this_work`` comes from ``inventory``, which agents/missed_work.py has
    already filtered to ``acceptance_policy.should_accept``, and the answer to
    "what would it take" is the remedy question from rules.yaml rather than an
    opinion this module is in no position to hold.
    """
    document = _missed_document(metrics)
    totals = document.get("totals") if isinstance(document.get("totals"), dict) else {}
    if not totals:
        return [], [], [], []

    missed = int(_number(totals.get("missed")) or 0)
    recoverable = int(_number(totals.get("recoverable")) or 0)
    offers = int(_number(totals.get("offers")) or 0)
    unanswered = int(_number(totals.get("no_response")) or 0)
    withdrew = int(_number(totals.get("withdrew")) or 0)

    sentences = [
        f"{missed} of {offers} offers were not won, {recoverable} of them recoverable "
        f"(client withdrawals, {withdrew}, are reported separately).",
    ]
    if unanswered:
        sentences.append(f"{unanswered} went unanswered -- nobody responded in time.")

    ranked: list[dict] = []
    for row in (document.get("inventory") or [])[:6]:
        if not isinstance(row, dict):
            continue
        row_missed = int(_number(row.get("missed")) or 0)
        ranked.append(
            {
                "work": str(row.get("service_class") or ""),
                "missed": row_missed,
                "cause": str(row.get("cause") or ""),
                "supporting_number": (
                    f"{row_missed} of {int(_number(row.get('offers')) or 0)} "
                    f"{row.get('service_class')} offers, ranked by job count"
                ),
            }
        )

    # A cause that appears in the inventory is, by construction, a cause of
    # missed work in a class the owner said he wants.
    wanted_causes = {str(row.get("cause") or "") for row in ranked}
    assessments: list[dict] = []
    by_cause = document.get("by_cause")
    if isinstance(by_cause, dict):
        for cause, entry in by_cause.items():
            if not isinstance(entry, dict):
                continue
            count = int(_number(entry.get("missed")) or 0)
            assessments.append(
                {
                    "cause": str(cause),
                    "want_this_work": "yes" if str(cause) in wanted_causes else "unclear",
                    "what_it_would_take": str(entry.get("question") or "")
                    or f"remedy class: {entry.get('remedy')}",
                    "supporting_number": f"{count} missed jobs, remedy {entry.get('remedy')}",
                }
            )
    assessments.sort(key=lambda item: item["cause"])

    close_off: list[dict] = []
    closeoff = document.get("closeoff_candidates")
    if isinstance(closeoff, dict):
        for client in (closeoff.get("clients") or [])[:5]:
            if not isinstance(client, dict):
                continue
            for row in (client.get("service_types") or [])[:3]:
                if not isinstance(row, dict):
                    continue
                close_off.append(
                    {
                        "client": str(client.get("client") or ""),
                        "service_type": str(row.get("service_type_raw") or ""),
                        "supporting_number": (
                            f"{int(_number(row.get('offers')) or 0)} offers, "
                            f"{int(_number(row.get('accepted')) or 0)} accepted"
                        ),
                    }
                )
    return ranked, assessments, close_off, sentences


def _fallback_analysis(metrics: dict, report_type: str, reason: str) -> dict:
    """A deterministic, fully sourced report for when the LLM is unavailable.

    Deliberately plain, and deliberately in the same ORDER as the LLM answer:
    what we missed first, acceptance rate last. It is better for the owner to
    receive the numbers with a flat sentence around them than to receive nothing
    because the commentary layer was down -- but a fallback that quietly
    reverted to leading with acceptance rate would be reporting the wrong thing
    on exactly the days the system is least healthy.
    """
    offered = metrics.get("offered")
    accepted = metrics.get("accepted")
    rate_text = _pct_text(accepted, offered)

    offered_n = _number(offered) or 0
    accepted_n = _number(accepted) or 0
    missed = int(offered_n - accepted_n)

    label = {
        "hourly": "This hour",
        "daily": "Today",
        "weekly": "This week",
        "monthly": "This month",
    }.get(report_type, "This window")

    ranked, assessments, close_off, missed_sentences = _fallback_missed_work(metrics)

    sentences: list[str] = []
    if missed_sentences:
        sentences.append(f"{label}: " + missed_sentences[0])
        sentences.extend(missed_sentences[1:])
        sentences.append(
            f"Acceptance rate was {rate_text} ({int(accepted_n)} of {int(offered_n)}). "
            "All figures are job counts; offer amounts are not available."
        )
    else:
        sentences.append(
            f"{label}: {int(offered_n)} jobs offered, {int(accepted_n)} accepted ({rate_text})."
        )
        if missed > 0:
            sentences.append(f"{missed} offer(s) were not accepted.")
        if offered_n == 0:
            sentences.append("No jobs were offered in this window.")

    trend_statements: list[dict] = []
    document = _missed_document(metrics)
    document_totals = document.get("totals") if isinstance(document.get("totals"), dict) else {}
    if document_totals:
        trend_statements.append(
            {
                "statement": "Work missed in this window, ranked by job count",
                "supporting_number": (
                    f"{int(_number(document_totals.get('missed')) or 0)} missed of "
                    f"{int(_number(document_totals.get('offers')) or 0)} offered, "
                    f"{int(_number(document_totals.get('recoverable')) or 0)} recoverable"
                ),
            }
        )
    # Period-over-period movement, when the metrics document carried a prior
    # period. compute_weekly and compute_monthly both emit `missed_work_trend`
    # with the direction already decided, so the fallback can answer question 5
    # from arithmetic instead of leaving the weekly and monthly reports -- the
    # two whose entire purpose is direction -- with nothing to say.
    movement = metrics.get("missed_work_trend")
    if isinstance(movement, dict):
        by_cause = movement.get("by_cause")
        if isinstance(by_cause, dict):
            rows = [row for row in by_cause.values() if isinstance(row, dict)]
            rows.sort(key=lambda row: -(_number(row.get("missed")) or 0))
            for row in rows[:6]:
                now = int(_number(row.get("missed")) or 0)
                was = int(_number(row.get("missed_prior")) or 0)
                if now == 0 and was == 0:
                    continue
                trend_statements.append(
                    {
                        "statement": (
                            f"{row.get('cause')} missed work is "
                            f"{row.get('direction') or 'flat'}"
                        ),
                        "supporting_number": f"{now} this period against {was} last",
                    }
                )
        resolved = (movement.get("closeoff_candidates") or {}).get("resolved") or []
        if resolved:
            trend_statements.append(
                {
                    "statement": (
                        "No longer offered often enough to be a close-off candidate "
                        "-- a close-off that took effect"
                    ),
                    "supporting_number": f"{len(resolved)} service type(s): "
                    + ", ".join(str(item) for item in resolved[:5]),
                }
            )
        closed = (movement.get("blind_spots") or {}).get("closed") or []
        if closed:
            trend_statements.append(
                {
                    "statement": "Blind spots that closed since last period",
                    "supporting_number": f"{len(closed)} window(s): "
                    + ", ".join(str(item) for item in closed[:5]),
                }
            )
        opened = (movement.get("blind_spots") or {}).get("opened") or []
        if opened:
            trend_statements.append(
                {
                    "statement": "Blind spots that opened since last period",
                    "supporting_number": f"{len(opened)} window(s): "
                    + ", ".join(str(item) for item in opened[:5]),
                }
            )

    trend_statements.append(
        {
            "statement": f"Acceptance rate for this {report_type} window",
            "supporting_number": f"{int(accepted_n)}/{int(offered_n)} ({rate_text})",
        }
    )

    service_classes = metrics.get("by_service_class") or metrics.get("service_classes") or {}
    if isinstance(service_classes, dict):
        for name, stats in list(service_classes.items())[:6]:
            if not isinstance(stats, dict):
                continue
            trend_statements.append(
                {
                    "statement": f"{name} acceptance",
                    "supporting_number": (
                        f"{int(_number(stats.get('accepted')) or 0)}/"
                        f"{int(_number(stats.get('offered')) or 0)} "
                        f"({_pct_text(stats.get('accepted'), stats.get('offered'))})"
                    ),
                }
            )

    clients_needing_attention: list[dict] = []
    # The unanswered-rate gap between two clients using the same trucks and the
    # same staff is the strongest finding this system produces, and
    # agents/missed_work.py has already stated it in a sourced sentence. Carry it
    # through verbatim rather than paraphrasing a number.
    for finding in (document.get("client_comparison") or {}).get("findings") or []:
        if not isinstance(finding, dict):
            continue
        worst = finding.get("worst") if isinstance(finding.get("worst"), dict) else {}
        best = finding.get("best") if isinstance(finding.get("best"), dict) else {}
        clients_needing_attention.append(
            {
                "client": str(worst.get("client") or "unknown"),
                "reason": str(finding.get("summary") or "offers go unanswered far more often"),
                "supporting_numbers": (
                    f"{int(_number(worst.get('no_response')) or 0)} of "
                    f"{int(_number(worst.get('offers')) or 0)} unanswered against "
                    f"{int(_number(best.get('no_response')) or 0)} of "
                    f"{int(_number(best.get('offers')) or 0)} for {best.get('client')} "
                    f"({finding.get('spread_pp')} points apart)"
                ),
            }
        )

    for row in _client_rows(metrics):
        row_offered = _number(row.get("offered")) or 0
        row_accepted = _number(row.get("accepted")) or 0
        if row_offered < 5:
            continue
        row_rate = row_accepted / row_offered if row_offered else 0.0
        if row_rate >= 0.60:
            continue
        name = row.get("client_name") or row.get("client_key") or "unknown"
        clients_needing_attention.append(
            {
                "client": str(name),
                "reason": "acceptance rate below 60% on 5 or more offers",
                "supporting_numbers": (
                    f"{int(row_accepted)}/{int(row_offered)} "
                    f"({_pct_text(row_accepted, row_offered)})"
                ),
            }
        )
    clients_needing_attention.sort(key=lambda item: item["client"])

    proposed_rules: list[dict] = []
    for text, count in _unclassified_samples(metrics)[:3]:
        if count < 5:
            continue
        name = _slug(text, "unclassified") or "unclassified"
        # Dumped rather than string-formatted: a service type containing a
        # comma or a bracket would otherwise produce a fragment that looks
        # fine and parses into something else entirely.
        fragment = yaml.safe_dump(
            {"service_classes": {name: {"match_any": [text.strip().lower()]}}},
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )
        proposed_rules.append(
            {
                "id": f"unclassified-{name}",
                "target": "service_classes",
                "rationale": (
                    f"The verbatim service type {text!r} appeared {count} time(s) in this "
                    f"{report_type} window and matched no service class, so it is counted "
                    "as unclassified. Decide which existing class it belongs to (or whether "
                    "it deserves its own) before pasting this in -- the suggested class name "
                    "is derived from the string, not from knowledge of the business."
                ),
                "service_type_raw_samples": [text],
                "occurrence_count": count,
                "patch_yaml": fragment,
            }
        )

    return {
        "narrative": " ".join(sentences),
        # The five missed-work answers, in the order MISSED_WORK_MODEL.md s9
        # asks them, so a degraded report reads the same way as a full one.
        "missed_work_ranked": ranked,
        "cause_assessments": assessments,
        "close_off_requests": close_off,
        "clients_needing_attention": clients_needing_attention,
        "trend_statements": trend_statements,
        "proposed_rules": proposed_rules,
        "llm": "unavailable",
        "llm_reason": reason,
    }


# ==========================================================================
# Public entry point (module contract)
# ==========================================================================


def analyze(
    metrics_obj: dict,
    report_type: str,
    *,
    persist_proposals: bool = True,
) -> dict:
    """Turn an aggregate metrics object into the missed-work answers.

    Returns::

        {
          "narrative": str,                  # leads with what we did NOT get
          "missed_work_ranked": [{work, missed, cause, supporting_number}],
          "cause_assessments": [{cause, want_this_work, what_it_would_take,
                                 supporting_number}],
          "close_off_requests": [{client, service_type, supporting_number}],
          "clients_needing_attention": [{client, reason, supporting_numbers}],
          "trend_statements": [{statement, supporting_number}],
          "proposed_rules": [...],
          "llm": "anthropic" | "unavailable",
          ...
        }

    The first five are MISSED_WORK_MODEL.md section 9's five questions, in
    order. Every entry in every list carries its supporting number, because
    :func:`_keep_sourced` drops the ones that do not -- and, while the feed
    carries no offer amounts, drops any that claim a dollar value.

    Never raises. Every failure path -- missing API key, network, refusal,
    unparseable JSON -- degrades to a deterministic narrative with
    ``"llm": "unavailable"`` that answers the same questions from the same
    numbers, because the commentary must never be the reason a report does not
    go out.
    """
    metrics_obj = dict(metrics_obj or {})
    report_type = str(report_type or "manual")

    # -- guard rail: aggregates only ----------------------------------------
    clean_metrics, removed = sanitize_metrics(metrics_obj)
    if removed:
        logger.warning(
            "stripped %d row-level or oversized field(s) before the analyst call: %s",
            len(removed),
            ", ".join(removed[:10]),
        )

    # Reported, NOT used as a switch. Whether the deterministic layer could
    # price anything is worth knowing; it has no bearing on whether a sentence
    # the model wrote may contain a dollar sign. See the module docstring, r3.
    revenue_available = _revenue_available(clean_metrics)

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    envelope: dict[str, Any] = {
        "report_type": report_type,
        "generated_at": generated_at,
        "rules_version": _safe_rules_version(),
        "model": None,
        "stripped_row_level_fields": removed,
        "dropped_unsourced_statements": 0,
        "dropped_money_claims": 0,
        "revenue_available": revenue_available,
        "ranking_basis": "job_count",
        "proposals_recorded": 0,
    }

    def _finish(result: dict) -> dict:
        """Common tail: enforce sourcing and the money rule, then persist."""
        dropped = 0
        for key, fields, label, number_field in _SOURCED_LISTS:
            kept, lost = _keep_sourced(
                result.get(key), fields, label=label, number_field=number_field
            )
            result[key] = kept
            dropped += lost

        result["proposed_rules"] = [
            item for item in (result.get("proposed_rules") or []) if isinstance(item, dict)
        ]

        narrative, money_sentences = _strip_money_sentences(result.get("narrative"))
        if money_sentences:
            logger.warning(
                "dropped %d narrative sentence(s) claiming a dollar value; every "
                "dollar figure in this system comes from the deterministic layer, "
                "never from the model",
                money_sentences,
            )
        result["narrative"] = narrative

        envelope["dropped_unsourced_statements"] = dropped
        envelope["dropped_money_claims"] = money_sentences
        if persist_proposals and result["proposed_rules"]:
            try:
                envelope["proposals_recorded"] = record_proposals(
                    result["proposed_rules"], report_type
                )
            except Exception as exc:  # pragma: no cover - filesystem level
                logger.error("could not record rule proposals: %s", exc)
                envelope["proposals_recorded"] = 0

        merged = {**envelope, **result}
        merged.setdefault("llm", "unavailable")
        return merged

    if not _api_key_present():
        logger.info("ANTHROPIC_API_KEY is not set; using the deterministic narrative")
        return _finish(_fallback_analysis(clean_metrics, report_type, "no_api_key"))

    user_prompt = _USER_TEMPLATE.format(
        report_type=report_type,
        rules_version=envelope["rules_version"],
        # The five questions are identical for every cadence -- they are the
        # missed-work model. What differs is what the reader does next, which
        # is what these two carry.
        cadence_brief=_cadence_brief(report_type),
        narrative_brief=_narrative_brief(report_type),
        service_classes=_service_class_summary(),
        should_accept=_should_accept_summary(),
        money_rule=_NO_MONEY_RULE,
        metrics=_serialise(compact_for_prompt(clean_metrics)),
    )

    try:
        parsed, model = _call_model(_SYSTEM_PROMPT, user_prompt)
    except AnalystError as exc:
        logger.error("analyst LLM call failed (%s); falling back to the template", exc)
        return _finish(_fallback_analysis(clean_metrics, report_type, str(exc)))
    except ImportError as exc:
        logger.error("anthropic SDK unavailable (%s); falling back to the template", exc)
        return _finish(_fallback_analysis(clean_metrics, report_type, f"sdk_missing: {exc}"))
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("unexpected analyst failure; falling back to the template")
        return _finish(_fallback_analysis(clean_metrics, report_type, f"{type(exc).__name__}: {exc}"))

    envelope["model"] = model
    parsed["llm"] = "anthropic"
    return _finish(parsed)
