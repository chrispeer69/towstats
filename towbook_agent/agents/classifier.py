"""Deterministic classification, acceptance policy and alert evaluation.

Hard constraint #3: exactly one component uses an LLM, and it is the Analyst.
This module is the opposite of that -- every function here is a pure, repeatable
function of its inputs and the YAML in config/rules.yaml. The same row and the
same rules always produce the same class, the same flags and the same alerts.

Hard constraint #2: the rules are DATA. Adding a service type or an alert is an
edit to config/rules.yaml, never a code change. Nothing in this file names a
service class, a match term, an acceptance policy or an alert condition. The
only literal class name is the ``unclassified`` fallback used when rules.yaml is
missing its mandatory ``_default``.

Hard constraint #8: alert ``when:`` expressions go through the sandboxed AST
evaluator in ``core.safe_eval``. There is no bare ``eval()`` anywhere.

Hot reload
----------
Rules are fetched through ``config_loader.get_rules()`` on *every* call, never
cached at import time. The loader re-stats the file, so saving rules.yaml takes
effect on the next classification with no restart. Callers that want a single
consistent snapshot across a batch may pass ``rules`` explicitly.

Classifier semantics (exact -- see the header comment in config/rules.yaml)
--------------------------------------------------------------------------
* Case-insensitive SUBSTRING test of each ``match_any`` term against
  ``service_type_raw``. "Heavy Duty Tow - Accident" matches the term ``tow``.
* FILE ORDER IS EVALUATION ORDER, first match wins. PyYAML preserves mapping
  order on 3.7+, so moving a block in the YAML changes precedence.
* ``_default: unclassified`` is mandatory. Keys beginning with ``_`` are
  directives, not service classes.
* ``service_type_raw`` is never modified. Matching works on a throwaway
  casefolded copy; the stored value is untouched (hard constraint #6).
"""

from __future__ import annotations

import ast
import logging
import re
from typing import Any, Iterable, Mapping, Sequence

from ..core.config_loader import get_rules
from ..core.safe_eval import (
    EvaluationError,
    SafeEvalError,
    UnknownName,
    UnsafeExpression,
    safe_eval,
    validate_expression,
)

__all__ = [
    "classify_service",
    "policy_flags",
    "evaluate_alerts",
    "backfill",
    "normalize_denial_reason",
    "service_class_names",
    "default_service_class",
    "UNCLASSIFIED",
]

logger = logging.getLogger(__name__)

#: Last-resort fallback used only when rules.yaml has no ``_default`` key.
#: ``_default`` is mandatory; an unknown service type is never silently bucketed
#: into a real class.
UNCLASSIFIED: str = "unclassified"

#: Keys inside ``service_classes`` that are directives rather than classes.
_DIRECTIVE_PREFIX = "_"
_DEFAULT_KEY = "_default"

#: Statuses that make an unfulfilled *should_accept* job a missed job, and the
#: statuses that count as taking a job. Overridable from rules.yaml via
#: ``acceptance_policy.missed_statuses`` / ``acceptance_policy.accepted_statuses``
#: so the policy stays data even though sensible defaults ship in code.
_DEFAULT_MISSED_STATUSES: tuple[str, ...] = ("denied", "expired")
_DEFAULT_ACCEPTED_STATUSES: tuple[str, ...] = ("accepted",)

_WHITESPACE = re.compile(r"\s+")

#: Warn once per distinct problem instead of once per row -- a 5000 row ingest
#: with a malformed rules.yaml must not produce 5000 identical log lines.
_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(message, *args)


# --------------------------------------------------------------------------
# Rule access
# --------------------------------------------------------------------------


def _resolve_rules(rules: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return the rules to use, re-reading config/rules.yaml when none given.

    This is what makes hot reload work: the default path asks the config loader
    every single call, and the loader reloads the file when its mtime changed.
    """
    if rules is None:
        return get_rules()
    if not isinstance(rules, Mapping):
        raise TypeError(f"rules must be a mapping, got {type(rules).__name__}")
    return rules


def _service_classes(rules: Mapping[str, Any]) -> Mapping[str, Any]:
    block = rules.get("service_classes")
    if not isinstance(block, Mapping):
        _warn_once(
            "no_service_classes",
            "rules.yaml has no service_classes mapping; every row will be %r",
            UNCLASSIFIED,
        )
        return {}
    return block


def default_service_class(rules: Mapping[str, Any] | None = None) -> str:
    """Return the mandatory ``_default`` class from rules.yaml."""
    block = _service_classes(_resolve_rules(rules))
    value = block.get(_DEFAULT_KEY)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, Mapping):  # tolerate `_default: {value: unclassified}`
        nested = value.get("value")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    if value is not None:
        _warn_once(
            "bad_default",
            "rules.yaml service_classes._default must be a string, got %r; using %r",
            value,
            UNCLASSIFIED,
        )
    else:
        _warn_once(
            "missing_default",
            "rules.yaml service_classes is missing the mandatory `_default` key; using %r",
            UNCLASSIFIED,
        )
    return UNCLASSIFIED


def service_class_names(rules: Mapping[str, Any] | None = None) -> list[str]:
    """Return the declared service classes in file order, plus the default.

    Used by the dashboard and the metrics aggregator so that a class with zero
    rows this week still shows up as a row with a zero in it.
    """
    block = _service_classes(_resolve_rules(rules))
    names = [key for key in block if not str(key).startswith(_DIRECTIVE_PREFIX)]
    fallback = default_service_class(rules)
    if fallback not in names:
        names.append(fallback)
    return names


def _match_terms(spec: Any, class_name: str) -> list[str]:
    """Extract the ``match_any`` terms from one service_classes entry.

    Accepts the documented ``{match_any: [...]}`` mapping and, as a
    convenience, a bare list shorthand.
    """
    if isinstance(spec, Mapping):
        terms = spec.get("match_any")
    elif isinstance(spec, (list, tuple)):
        terms = spec
    else:
        terms = None

    if terms is None:
        _warn_once(
            f"no_match_any:{class_name}",
            "rules.yaml service_class %r has no match_any list; it can never match",
            class_name,
        )
        return []
    if isinstance(terms, str):  # `match_any: tow` -- tolerate the scalar form
        terms = [terms]
    if not isinstance(terms, (list, tuple)):
        _warn_once(
            f"bad_match_any:{class_name}",
            "rules.yaml service_class %r has a non-list match_any (%s); ignoring it",
            class_name,
            type(terms).__name__,
        )
        return []

    out: list[str] = []
    for term in terms:
        if term is None:
            continue
        text = _fold(str(term))
        if text:
            out.append(text)
    return out


def _fold(value: Any) -> str:
    """Casefold and collapse whitespace for matching purposes only.

    The stored ``service_type_raw`` is never touched: this operates on a copy.
    Collapsing runs of whitespace means an export that writes "Winch  Out" with
    a double space still matches the term "winch out".
    """
    if value is None:
        return ""
    # Web exports leak non-breaking and narrow no-break spaces; \s does not
    # match U+00A0 in a str pattern, so map them to a plain space first.
    text = str(value).replace("\u00a0", " ").replace("\u202f", " ")
    return _WHITESPACE.sub(" ", text).strip().casefold()


# --------------------------------------------------------------------------
# classify_service
# --------------------------------------------------------------------------


def classify_service(service_type_raw: str, rules: Mapping[str, Any] | None = None) -> str:
    """Map a verbatim service-type string to a service class.

    Case-insensitive substring match of each ``match_any`` term against
    ``service_type_raw``. File order is evaluation order and the first match
    wins. Anything that matches nothing gets ``_default`` (``unclassified``) --
    unknowns are never quietly folded into a real class, because an
    unclassified job is a signal, not noise.

    ``rules`` is optional; when omitted the current config/rules.yaml is read
    through the hot-reloading loader, so an edit takes effect immediately.
    """
    resolved = _resolve_rules(rules)
    fallback = default_service_class(resolved)

    haystack = _fold(service_type_raw)
    if not haystack:
        return fallback

    for class_name, spec in _service_classes(resolved).items():
        name = str(class_name)
        if name.startswith(_DIRECTIVE_PREFIX):
            continue  # `_default` and any future directive
        for term in _match_terms(spec, name):
            if term in haystack:
                return name

    return fallback


# --------------------------------------------------------------------------
# denial_reason_normalization (optional, ships empty)
# --------------------------------------------------------------------------


def normalize_denial_reason(
    denial_reason: str | None, rules: Mapping[str, Any] | None = None
) -> str | None:
    """Bucket a free-text denial reason, if rules.yaml declares any buckets.

    Same semantics as the classifier: case-insensitive substring match, list
    order is evaluation order, first match wins. Ships empty on purpose --
    until real denial reasons have been observed, inventing buckets would
    invent categories that are not in the data. Returns None for an empty
    reason and the raw text unchanged when nothing matches, so the caller can
    group the leftovers under "other" without losing the original wording.
    """
    resolved = _resolve_rules(rules)
    haystack = _fold(denial_reason)
    if not haystack:
        return None

    buckets = resolved.get("denial_reason_normalization") or []
    if not isinstance(buckets, (list, tuple)):
        _warn_once(
            "bad_denial_buckets",
            "rules.yaml denial_reason_normalization must be a list, got %s; ignoring",
            type(buckets).__name__,
        )
        return denial_reason

    for entry in buckets:
        if not isinstance(entry, Mapping):
            continue
        normalized = entry.get("normalized")
        if not normalized:
            continue
        for term in _match_terms(entry, str(normalized)):
            if term in haystack:
                return str(normalized)

    return denial_reason


# --------------------------------------------------------------------------
# policy_flags
# --------------------------------------------------------------------------


def _status_set(policy: Mapping[str, Any], key: str, default: Sequence[str]) -> frozenset[str]:
    value = policy.get(key, default)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        value = default
    return frozenset(_fold(item) for item in value if item is not None)


def _predicate_matches(predicate: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    """True when every field/value pair in ``predicate`` matches ``row``.

    Written generically rather than as ``predicate["service_class"]`` so that a
    future policy entry such as ``{service_class: tow, client_key: acme}`` or
    ``{service_class: [tow, winch_out]}`` works with no code change.
    """
    if not predicate:
        return False
    for field, expected in predicate.items():
        actual = row.get(str(field))
        if isinstance(expected, (list, tuple, set, frozenset)):
            if _fold(actual) not in {_fold(item) for item in expected}:
                return False
        elif _fold(actual) != _fold(expected):
            return False
    return True


def _matches_any(entries: Any, row: Mapping[str, Any]) -> bool:
    if not isinstance(entries, (list, tuple)):
        return False
    for entry in entries:
        if isinstance(entry, Mapping) and _predicate_matches(entry, row):
            return True
        # Shorthand: a bare string in the list means a service class name.
        if isinstance(entry, str) and _fold(entry) == _fold(row.get("service_class")):
            return True
    return False


def policy_flags(
    row: Mapping[str, Any], rules: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Apply ``acceptance_policy`` to one request row.

    Returns the three contract keys plus a little detail the dashboard uses:

    ``should_accept``  -- policy says take this job
    ``should_decline`` -- policy says pass on this job
    ``policy_violation`` -- what actually happened contradicts the policy:
        a ``should_accept`` job that ended denied/expired is a **missed job**
        (lost revenue), and a ``should_decline`` job that was accepted is an
        **against-policy acceptance** (a truck tied up on a low-value call).

    ``row`` needs ``status`` and either ``service_class`` or
    ``service_type_raw``; the class is derived when it is absent so this works
    on a freshly parsed sheet row as well as on a stored Request.
    """
    resolved = _resolve_rules(rules)

    service_class = row.get("service_class")
    if not service_class:
        service_class = classify_service(row.get("service_type_raw") or "", resolved)

    status = _fold(row.get("status"))
    subject: dict[str, Any] = dict(row)
    subject["service_class"] = service_class

    policy = resolved.get("acceptance_policy")
    if not isinstance(policy, Mapping):
        if policy is not None:
            _warn_once(
                "bad_policy",
                "rules.yaml acceptance_policy must be a mapping, got %s; treating as empty",
                type(policy).__name__,
            )
        policy = {}

    should_accept = _matches_any(policy.get("should_accept"), subject)
    should_decline = _matches_any(policy.get("should_decline"), subject)

    if should_accept and should_decline:
        _warn_once(
            f"policy_conflict:{service_class}",
            "rules.yaml acceptance_policy lists service_class %r under BOTH "
            "should_accept and should_decline; should_accept wins",
            service_class,
        )
        should_decline = False

    # A class named in neither list may still carry an `accept:` flag on its
    # service_classes block (winch_out ships `accept: true`). Honour it as a
    # fallback so both spellings of the same intent agree.
    if not should_accept and not should_decline:
        spec = _service_classes(resolved).get(service_class)
        if isinstance(spec, Mapping) and isinstance(spec.get("accept"), bool):
            should_accept = spec["accept"]
            should_decline = not spec["accept"]

    missed_statuses = _status_set(policy, "missed_statuses", _DEFAULT_MISSED_STATUSES)
    accepted_statuses = _status_set(policy, "accepted_statuses", _DEFAULT_ACCEPTED_STATUSES)

    missed_job = bool(should_accept and status in missed_statuses)
    against_policy_acceptance = bool(should_decline and status in accepted_statuses)

    violation: str | None = None
    if missed_job:
        violation = "missed_job"
    elif against_policy_acceptance:
        violation = "against_policy_acceptance"

    return {
        "service_class": service_class,
        "status": status or None,
        "should_accept": should_accept,
        "should_decline": should_decline,
        "policy_violation": violation is not None,
        "violation": violation,
        "missed_job": missed_job,
        "against_policy_acceptance": against_policy_acceptance,
    }


# --------------------------------------------------------------------------
# evaluate_alerts
# --------------------------------------------------------------------------


def _expression_names(expr: str) -> list[str]:
    """Names referenced by ``expr``, so a firing alert can carry its evidence."""
    try:
        tree = validate_expression(expr)
    except SafeEvalError:
        return []
    seen: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in seen:
            seen.append(node.id)
    return seen


def _entity_for(alert: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    """Resolve the thing an alert fired about.

    ``entity`` in the rule names a context field (``entity: client_key``). When
    it is absent the usual suspects are tried in order, and a global alert with
    none of them present gets "" -- which is exactly what the
    same_alert_same_entity rate limit expects.
    """
    declared = alert.get("entity")
    if isinstance(declared, str) and declared:
        value = context.get(declared, declared)
        return "" if value is None else str(value)
    for field in ("client_key", "request_id", "service_class", "account_id"):
        value = context.get(field)
        if value:
            return str(value)
    return ""


def evaluate_alerts(
    context: Mapping[str, Any], rules: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Evaluate every alert in rules.yaml against ``context``.

    Each ``when:`` is evaluated by ``core.safe_eval`` -- an AST whitelist, never
    a bare ``eval()``. A rule that references a name the current context does
    not carry does **not** fire and does **not** raise: the hourly context has
    no ``client_acceptance_rate_24h``, and that is normal, not an error. It is
    logged at DEBUG.

    Returns one dict per firing alert, in file order:

        {"id", "severity", "when", "entity", "context"}

    where ``context`` is only the subset of names the expression actually read,
    which is what makes the resulting SMS say *why* it fired.
    """
    resolved = _resolve_rules(rules)
    alerts = resolved.get("alerts") or []
    if not isinstance(alerts, (list, tuple)):
        _warn_once(
            "bad_alerts",
            "rules.yaml alerts must be a list, got %s; no alerts will fire",
            type(alerts).__name__,
        )
        return []

    context = dict(context or {})
    firing: list[dict[str, Any]] = []

    for index, alert in enumerate(alerts):
        if not isinstance(alert, Mapping):
            _warn_once(f"bad_alert:{index}", "rules.yaml alerts[%d] is not a mapping; skipped", index)
            continue

        alert_id = str(alert.get("id") or f"alert_{index}")
        expression = alert.get("when")
        if not isinstance(expression, str) or not expression.strip():
            _warn_once(
                f"no_when:{alert_id}",
                "alert %r has no `when` expression; it can never fire",
                alert_id,
            )
            continue

        try:
            result = safe_eval(expression, context)
        except UnknownName as exc:
            # Expected and harmless: this context simply does not carry that
            # metric. Never let it raise, never let it fire.
            logger.debug("alert %s not evaluated: %s", alert_id, exc)
            continue
        except UnsafeExpression as exc:
            # A config bug, and a potentially dangerous one. Loud, but the
            # pipeline keeps going.
            _warn_once(
                f"unsafe:{alert_id}",
                "alert %r has an unsafe `when` expression and was skipped: %s",
                alert_id,
                exc,
            )
            continue
        except EvaluationError as exc:
            _warn_once(
                f"eval_error:{alert_id}",
                "alert %r could not be evaluated (%s); treated as not firing",
                alert_id,
                exc,
            )
            continue
        except SafeEvalError as exc:  # pragma: no cover - defensive
            logger.debug("alert %s not evaluated: %s", alert_id, exc)
            continue

        if not result:
            continue

        used = _expression_names(expression)
        firing.append(
            {
                "id": alert_id,
                "severity": str(alert.get("severity") or "medium"),
                "when": expression,
                "entity": _entity_for(alert, context),
                "context": {name: context[name] for name in used if name in context},
            }
        )

    return firing


# --------------------------------------------------------------------------
# backfill
# --------------------------------------------------------------------------

#: Rows per UPDATE statement. Deliberately small: SQLite builds before 3.32
#: cap bound parameters at 999, and this keeps one statement well inside that.
_UPDATE_CHUNK: int = 400
_SCAN_BATCH: int = 2000


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def backfill(session: Any, company_id: str | None = None) -> int:
    """Re-derive ``service_class`` for stored rows and return the count changed.

    This is the payoff of hard constraint #6. ``service_type_raw`` was stored
    verbatim and never mutated, so a rules.yaml edit -- a new match term, a
    reordered class -- can be applied retroactively to history instead of only
    to rows ingested from now on.

    PER COMPANY, and not as a filter bolted on afterwards. A company entry in
    config/companies.yaml may override ``service_classes``, so "what class is
    this row" has a different answer for different tenants over the identical
    verbatim string. Classifying one company's rows under another's rules would
    corrupt the stored column silently and permanently.

    ``company_id=None`` therefore does not mean "one pass over everything": it
    runs one pass per company present in the data, each under its own rules.
    On a single-company install that is exactly one pass and identical to the
    behaviour before companies existed.

    Idempotent: it writes only rows whose class actually differs, so a second
    run over unchanged rules returns 0.
    """
    from sqlalchemy import select

    from ..core.models import Request

    if company_id is None:
        company_ids = [
            row[0]
            for row in session.execute(
                select(Request.company_id).distinct().order_by(Request.company_id)
            )
            if row[0]
        ]
        if len(company_ids) > 1:
            return sum(_backfill_company(session, one) for one in company_ids)
        company_id = company_ids[0] if company_ids else None

    return _backfill_company(session, company_id)


def _backfill_company(session: Any, company_id: str | None) -> int:
    """One reclassification pass over one company's rows, under its own rules."""
    from sqlalchemy import select, update

    from ..core import companies as _companies
    from ..core.models import Request

    # One snapshot for the whole pass, deliberately: a human saving rules.yaml
    # mid-backfill must not produce a table classified under two rule sets.
    rules = _companies.rules_for(company_id) or get_rules()
    fallback = default_service_class(rules)

    # Materialise the plan before writing anything: mutating rows while a
    # cursor is still streaming from the same table is asking for trouble on
    # SQLite, and the id/raw pairs are small.
    statement = select(Request.request_id, Request.service_type_raw, Request.service_class)
    if company_id is not None:
        statement = statement.where(Request.company_id == company_id)
    rows = session.execute(statement.execution_options(yield_per=_SCAN_BATCH)).all()

    pending: dict[str, list[str]] = {}
    scanned = 0
    for request_id, service_type_raw, current_class in rows:
        scanned += 1
        new_class = classify_service(service_type_raw or "", rules)
        if new_class == current_class:
            continue
        pending.setdefault(new_class, []).append(request_id)

    changed = 0
    for new_class, ids in pending.items():
        for chunk in _chunks(ids, _UPDATE_CHUNK):
            session.execute(
                update(Request)
                .where(Request.request_id.in_(list(chunk)))
                .values(service_class=new_class)
                .execution_options(synchronize_session=False)
            )
            changed += len(chunk)
        session.flush()

    if changed:
        session.commit()

    logger.info(
        "backfill scanned %d row(s) for company %s, reclassified %d, default class %r",
        scanned,
        company_id or "(all)",
        changed,
        fallback,
    )
    return changed
