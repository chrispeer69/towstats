"""Sandboxed expression evaluator for the ``when:`` clauses in config/rules.yaml.

Alert conditions are data, written by a human into YAML, and they must be able
to change without a code change. That means they have to be evaluated at
runtime -- but ``eval()`` on a config file is a remote code execution hole, so
this module walks a whitelisted AST instead.

    >>> safe_eval("offers >= 10 and rate < 0.6", {"offers": 12, "rate": 0.5})
    True

Whitelist (anything else raises UnsafeExpression):

    Expression, BoolOp, UnaryOp, Compare, Name, Load, Constant,
    And, Or, Not, Eq, NotEq, Lt, LtE, Gt, GtE, In, NotIn,
    BinOp, Add, Sub, Mult, Div, List, Tuple, Set

Names resolve from the supplied context dict and nothing else. Builtins are
never reachable: there are no Call, Attribute or Subscript nodes in the
whitelist, so there is no syntax that could reach an attribute, call a
function, or index into an object.
"""

from __future__ import annotations

import ast
from typing import Any, Callable, Final, Mapping

__all__ = [
    "safe_eval",
    "safe_eval_bool",
    "validate_expression",
    "SafeEvalError",
    "UnsafeExpression",
    "UnknownName",
    "EvaluationError",
    "ALLOWED_NODES",
]


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class SafeEvalError(Exception):
    """Base class for every failure raised by this module."""


class UnsafeExpression(SafeEvalError):
    """The expression used syntax that is not on the whitelist."""


class UnknownName(SafeEvalError):
    """The expression referenced a name that is not present in the context."""


class EvaluationError(SafeEvalError):
    """The expression was well formed but blew up while being evaluated."""


# --------------------------------------------------------------------------
# Whitelist
# --------------------------------------------------------------------------

ALLOWED_NODES: Final[frozenset[type[ast.AST]]] = frozenset(
    {
        ast.Expression,
        ast.BoolOp,
        ast.UnaryOp,
        ast.Compare,
        ast.Name,
        ast.Load,
        ast.Constant,
        ast.And,
        ast.Or,
        ast.Not,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.In,
        ast.NotIn,
        ast.BinOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.List,
        ast.Tuple,
        ast.Set,
        # ASSUMPTION: USub/UAdd are added to the spec list because Python parses
        # a negative literal such as `-1` as UnaryOp(USub, Constant(1)). Without
        # them, "delta > -5" would be rejected. They cannot reach anything.
        ast.USub,
        ast.UAdd,
    }
)

# Only these literal types may appear as constants. Notably excludes Ellipsis
# and complex numbers, which have no business in an alert rule.
_ALLOWED_CONSTANTS: Final[tuple[type, ...]] = (str, int, float, bool, type(None))

# Guard rails against pathological config.
_MAX_EXPRESSION_LENGTH: Final[int] = 2000
_MAX_DEPTH: Final[int] = 40

_BOOL_OPS: Final[dict[type[ast.AST], str]] = {ast.And: "and", ast.Or: "or"}


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _node_name(node: ast.AST) -> str:
    return type(node).__name__


def validate_expression(expr: str) -> ast.Expression:
    """Parse ``expr`` and verify every node is whitelisted.

    Returns the parsed AST so callers can cache it. Raises UnsafeExpression for
    anything that is not allowed, including syntax errors.
    """
    if not isinstance(expr, str):
        raise UnsafeExpression(f"expression must be a string, got {type(expr).__name__}")
    if len(expr) > _MAX_EXPRESSION_LENGTH:
        raise UnsafeExpression(
            f"expression is {len(expr)} characters, limit is {_MAX_EXPRESSION_LENGTH}"
        )
    if not expr.strip():
        raise UnsafeExpression("expression is empty")

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:  # pragma: no cover - message varies by version
        raise UnsafeExpression(f"could not parse expression: {exc}") from exc

    for node in ast.walk(tree):
        if type(node) not in ALLOWED_NODES:
            raise UnsafeExpression(
                f"{_node_name(node)} is not allowed in a rule expression: {expr!r}"
            )
        if isinstance(node, ast.Constant) and not isinstance(node.value, _ALLOWED_CONSTANTS):
            raise UnsafeExpression(
                f"constant of type {type(node.value).__name__} is not allowed: {expr!r}"
            )

    _check_depth(tree, 0, expr)
    return tree


def _check_depth(node: ast.AST, depth: int, expr: str) -> None:
    if depth > _MAX_DEPTH:
        raise UnsafeExpression(f"expression nests deeper than {_MAX_DEPTH} levels: {expr!r}")
    for child in ast.iter_child_nodes(node):
        _check_depth(child, depth + 1, expr)


# A small parse cache. Expressions come from a config file that is re-read on
# every access, so the same handful of strings is parsed over and over.
_PARSE_CACHE: dict[str, ast.Expression] = {}
_PARSE_CACHE_LIMIT: Final[int] = 512


def _parse_cached(expr: str) -> ast.Expression:
    cached = _PARSE_CACHE.get(expr)
    if cached is not None:
        return cached
    tree = validate_expression(expr)
    if len(_PARSE_CACHE) >= _PARSE_CACHE_LIMIT:
        _PARSE_CACHE.clear()
    _PARSE_CACHE[expr] = tree
    return tree


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def _eval_node(node: ast.AST, context: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, context)

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        try:
            return context[node.id]
        except KeyError:
            raise UnknownName(f"name {node.id!r} is not available in the rule context") from None

    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for value_node in node.values:
                result = _eval_node(value_node, context)
                if not result:
                    return result
            return result
        # Or
        result = False
        for value_node in node.values:
            result = _eval_node(value_node, context)
            if result:
                return result
        return result

    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, context)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return _guard(lambda: -operand, "unary -")
        if isinstance(node.op, ast.UAdd):
            return _guard(lambda: +operand, "unary +")
        raise UnsafeExpression(f"unary operator {_node_name(node.op)} is not allowed")

    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, context)
        right = _eval_node(node.right, context)
        if isinstance(node.op, ast.Add):
            return _guard(lambda: left + right, "+")
        if isinstance(node.op, ast.Sub):
            return _guard(lambda: left - right, "-")
        if isinstance(node.op, ast.Mult):
            return _guard(lambda: left * right, "*")
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise EvaluationError("division by zero in rule expression")
            return _guard(lambda: left / right, "/")
        raise UnsafeExpression(f"binary operator {_node_name(node.op)} is not allowed")

    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, context)
        for op, right_node in zip(node.ops, node.comparators):
            right = _eval_node(right_node, context)
            if not _compare(op, left, right):
                return False
            left = right  # supports chained comparisons: 0 < rate < 1
        return True

    if isinstance(node, ast.List):
        return [_eval_node(item, context) for item in node.elts]

    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(item, context) for item in node.elts)

    if isinstance(node, ast.Set):
        return {_eval_node(item, context) for item in node.elts}

    raise UnsafeExpression(f"{_node_name(node)} is not allowed in a rule expression")


def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Eq):
        return bool(left == right)
    if isinstance(op, ast.NotEq):
        return bool(left != right)
    if isinstance(op, ast.Lt):
        return bool(_guard(lambda: left < right, "<"))
    if isinstance(op, ast.LtE):
        return bool(_guard(lambda: left <= right, "<="))
    if isinstance(op, ast.Gt):
        return bool(_guard(lambda: left > right, ">"))
    if isinstance(op, ast.GtE):
        return bool(_guard(lambda: left >= right, ">="))
    if isinstance(op, ast.In):
        return bool(_guard(lambda: left in right, "in"))
    if isinstance(op, ast.NotIn):
        return bool(_guard(lambda: left not in right, "not in"))
    raise UnsafeExpression(f"comparison operator {_node_name(op)} is not allowed")


def _guard(fn: Callable[[], Any], label: str) -> Any:
    """Run an operator and convert Python-level errors into EvaluationError.

    Comparing None to a number, for example, is a config bug rather than a
    crash-the-pipeline event; the caller decides what to do about it.
    """
    try:
        return fn()
    except SafeEvalError:
        raise
    except Exception as exc:
        raise EvaluationError(f"{label} failed in rule expression: {exc}") from exc


def safe_eval(expr: str, context: dict) -> Any:
    """Evaluate ``expr`` against ``context`` and return the result.

    Raises UnsafeExpression for disallowed syntax, UnknownName for a name that
    is missing from the context, and EvaluationError for a runtime failure.
    """
    if context is None:
        context = {}
    tree = _parse_cached(expr)
    return _eval_node(tree, context)


def safe_eval_bool(expr: str, context: dict, default: bool = False) -> bool:
    """Evaluate ``expr`` and coerce the result to a bool, never raising.

    Convenience wrapper for alert evaluation, where a rule that references a
    metric the current context does not carry should simply not fire rather
    than take the pipeline down. The failure is returned as ``default``; the
    caller is expected to log it.
    """
    try:
        return bool(safe_eval(expr, context))
    except SafeEvalError:
        return default
