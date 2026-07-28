"""The alert sandbox: what it must accept, and everything it must refuse.

Hard constraint #8. ``when:`` expressions come out of a YAML file that a human
edits, so they are evaluated at runtime -- which makes the evaluator the one
place in this system where a mistake is a remote code execution hole rather
than a wrong number. The refusal tests below are the security boundary.
"""

from __future__ import annotations

import pytest

from towbook_agent.core.config_loader import get_rules
from towbook_agent.core.safe_eval import (
    EvaluationError,
    UnknownName,
    UnsafeExpression,
    safe_eval,
    safe_eval_bool,
    validate_expression,
)

# --------------------------------------------------------------------------
# Refusals -- the whole point of the module
# --------------------------------------------------------------------------

ATTRIBUTE_ACCESS = [
    "x.real",
    "x.__class__",
    "''.__class__",
    "x.__class__.__mro__",
    "().__class__.__bases__",
]

CALLS = [
    "len(x)",
    "print('hi')",
    "open('secrets.txt')",
    "exit()",
    "x()",
    "type(x)",
]

IMPORTS_AND_DUNDERS = [
    "__import__('os')",
    "__import__('os').system('calc')",
    "().__class__.__base__.__subclasses__()",
    "x.__dict__",
]

#: Bare dunder *names* are syntactically ordinary, so they are refused one step
#: later: they resolve from the context and nowhere else, and the context never
#: contains them. Either way they are unreachable.
BARE_DUNDER_NAMES = ["__builtins__", "__name__", "__globals__"]

COMPREHENSIONS_AND_FLOW = [
    "[i for i in range(3)]",
    "{i: i for i in range(3)}",
    "{i for i in range(3)}",
    "(i for i in range(3))",
    "lambda: 1",
    "x if x else 0",
    "(y := 3)",
]

SUBSCRIPT_AND_FORMAT = [
    "x[0]",
    "x['key']",
    "f'{x}'",
    "'%s' % x",
    "x ** 2",
    "x @ x",
]


@pytest.mark.parametrize("expr", ATTRIBUTE_ACCESS)
def test_attribute_access_is_rejected(expr: str) -> None:
    with pytest.raises(UnsafeExpression):
        safe_eval(expr, {"x": 1})


@pytest.mark.parametrize("expr", CALLS)
def test_calls_are_rejected(expr: str) -> None:
    with pytest.raises(UnsafeExpression):
        safe_eval(expr, {"x": 1, "len": len, "print": print})


@pytest.mark.parametrize("expr", IMPORTS_AND_DUNDERS)
def test_imports_and_dunder_access_are_rejected(expr: str) -> None:
    with pytest.raises(UnsafeExpression):
        safe_eval(expr, {"x": 1})


@pytest.mark.parametrize("expr", BARE_DUNDER_NAMES)
def test_bare_dunder_names_do_not_resolve(expr: str) -> None:
    with pytest.raises(UnknownName):
        safe_eval(expr, {"x": 1})


@pytest.mark.parametrize("expr", COMPREHENSIONS_AND_FLOW)
def test_comprehensions_and_control_flow_are_rejected(expr: str) -> None:
    with pytest.raises(UnsafeExpression):
        safe_eval(expr, {"x": 1})


@pytest.mark.parametrize("expr", SUBSCRIPT_AND_FORMAT)
def test_subscripting_and_formatting_are_rejected(expr: str) -> None:
    with pytest.raises(UnsafeExpression):
        safe_eval(expr, {"x": [1, 2, 3]})


def test_builtins_are_unreachable_even_when_injected() -> None:
    """A name in the context is data, not a doorway.

    Passing ``len`` in the context makes the *name* resolvable, but there is no
    Call node in the whitelist, so it can never be invoked.
    """
    context = {"len": len, "__builtins__": __builtins__}
    assert safe_eval("len == len", context) is True
    with pytest.raises(UnsafeExpression):
        safe_eval("len('abc') > 2", context)


def test_statements_are_rejected() -> None:
    for expr in ("import os", "x = 1", "del x", "assert x", "raise Exception"):
        with pytest.raises(UnsafeExpression):
            safe_eval(expr, {"x": 1})


def test_syntax_error_is_an_unsafe_expression_not_a_crash() -> None:
    with pytest.raises(UnsafeExpression):
        safe_eval("client_rate <", {"client_rate": 1})


def test_unknown_name_raises_unknown_name() -> None:
    with pytest.raises(UnknownName):
        safe_eval("missing_metric > 1", {"present": 1})


def test_division_by_zero_is_an_evaluation_error() -> None:
    with pytest.raises(EvaluationError):
        safe_eval("offered / 0 > 1", {"offered": 5})


# --------------------------------------------------------------------------
# The expressions that actually ship
# --------------------------------------------------------------------------


def test_shipped_alert_expressions_all_validate() -> None:
    """Every ``when:`` in the shipped rules.yaml must parse and be whitelisted.

    If this fails, an alert would be silently dead in production.
    """
    alerts = get_rules().get("alerts") or []
    assert alerts, "rules.yaml ships no alerts"
    for alert in alerts:
        validate_expression(alert["when"])


def test_client_acceptance_drop_expression() -> None:
    expr = "client_acceptance_rate_24h < 0.60 and client_offers_24h >= 10"

    assert safe_eval(expr, {"client_acceptance_rate_24h": 0.42, "client_offers_24h": 12}) is True
    # Rate is bad but the sample is too small to mean anything.
    assert safe_eval(expr, {"client_acceptance_rate_24h": 0.42, "client_offers_24h": 9}) is False
    # Plenty of volume, healthy rate.
    assert safe_eval(expr, {"client_acceptance_rate_24h": 0.75, "client_offers_24h": 40}) is False
    # Boundary: 0.60 is not below 0.60, and 10 is >= 10.
    assert safe_eval(expr, {"client_acceptance_rate_24h": 0.60, "client_offers_24h": 10}) is False
    assert safe_eval(expr, {"client_acceptance_rate_24h": 0.599, "client_offers_24h": 10}) is True


def test_missed_tow_expression() -> None:
    expr = "service_class == 'tow' and status in ['denied','expired']"

    assert safe_eval(expr, {"service_class": "tow", "status": "denied"}) is True
    assert safe_eval(expr, {"service_class": "tow", "status": "expired"}) is True
    assert safe_eval(expr, {"service_class": "tow", "status": "accepted"}) is False
    assert safe_eval(expr, {"service_class": "tow", "status": "canceled"}) is False
    assert safe_eval(expr, {"service_class": "light_service", "status": "denied"}) is False


def test_unclassified_service_type_expression() -> None:
    expr = "service_class == 'unclassified'"

    assert safe_eval(expr, {"service_class": "unclassified"}) is True
    assert safe_eval(expr, {"service_class": "tow"}) is False


# --------------------------------------------------------------------------
# Expressions an operator might reasonably write next
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "context", "expected"),
    [
        ("offered >= 10 and rate < 0.6", {"offered": 12, "rate": 0.5}, True),
        ("not accepted", {"accepted": False}, True),
        ("a or b", {"a": False, "b": True}, True),
        ("status not in ['accepted', 'pending']", {"status": "denied"}, True),
        ("0 < rate < 1", {"rate": 0.5}, True),
        ("0 < rate < 1", {"rate": 1.5}, False),
        ("accepted / offered < 0.5", {"accepted": 4, "offered": 10}, True),
        ("delta > -5", {"delta": -1}, True),
        ("(offered - accepted) > 3", {"offered": 10, "accepted": 6}, True),
        ("severity in ('high', 'medium')", {"severity": "high"}, True),
        ("client in {'agero', 'quest'}", {"client": "agero"}, True),
    ],
)
def test_useful_expressions_evaluate(expr: str, context: dict, expected: bool) -> None:
    assert bool(safe_eval(expr, context)) is expected


# --------------------------------------------------------------------------
# safe_eval_bool: a rule that cannot be evaluated must not take the run down
# --------------------------------------------------------------------------


def test_safe_eval_bool_never_raises() -> None:
    # Missing name -> the rule simply does not fire.
    assert safe_eval_bool("nothing_here > 1", {}) is False
    # Unsafe syntax -> refused, still no exception.
    assert safe_eval_bool("__import__('os')", {}) is False
    # Explicit default for the "assume it fired" case.
    assert safe_eval_bool("nothing_here > 1", {}, default=True) is True
    # And it still evaluates real expressions.
    assert safe_eval_bool("offers >= 10", {"offers": 11}) is True


def test_oversized_expression_is_rejected() -> None:
    with pytest.raises(UnsafeExpression):
        safe_eval("x == 1 and " * 500 + "x == 1", {"x": 1})
