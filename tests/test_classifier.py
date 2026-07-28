"""Classification is data-driven, deterministic, and hot-reloadable.

The classifier is the piece most likely to be edited by a non-programmer: a new
service type shows up in the Request Log and someone adds a line to
``rules.yaml``. These tests pin the exact semantics that promise depends on --
case-insensitive substring matching, file order as evaluation order, a
mandatory ``unclassified`` fallback -- and prove that an edit takes effect with
no restart (hard constraint #2).
"""

from __future__ import annotations

from typing import Any

import pytest

from conftest import dig  # noqa: F401  (re-exported for symmetry with other modules)
from towbook_agent.core.config_loader import get_rules, rules_version

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _flag(flags: dict[str, Any], *names: str, default: Any = None) -> Any:
    """Read the first present key. ``policy_flags`` returns a dict; the spec
    fixes the signature but not every key name, so the unambiguous ones
    (``should_accept`` / ``should_decline``, straight out of the
    ``acceptance_policy`` block) are asserted by name and the rest tolerantly.
    """
    for name in names:
        if name in flags:
            return flags[name]
    return default


def _alert_id(alert: Any) -> str:
    if isinstance(alert, dict):
        for key in ("alert_id", "id", "name"):
            if key in alert:
                return str(alert[key])
        raise AssertionError(f"alert dict carries no id: {sorted(alert)}")
    return str(getattr(alert, "alert_id", getattr(alert, "id", alert)))


def _alert_ids(alerts: Any) -> list[str]:
    assert isinstance(alerts, list), f"evaluate_alerts must return a list, got {type(alerts)}"
    return [_alert_id(alert) for alert in alerts]


def _rules_with_classes(classes: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """A copy of the shipped rules with ``service_classes`` replaced."""
    rules = dict(base)
    rules["service_classes"] = classes
    return rules


# --------------------------------------------------------------------------
# substring + case semantics
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # exact
        ("Tow", "tow"),
        ("Winch Out", "winch_out"),
        ("Tire Change", "light_service"),
        # substring, with the match buried in the middle
        ("Heavy Duty Tow - Accident", "tow"),
        ("Light Duty Tow", "tow"),
        ("Flatbed Tow", "tow"),
        ("Customer needs a jump start, no keys lost", "light_service"),
        ("Vehicle Stuck In Ditch", "winch_out"),
        ("Winch-Out / Extraction", "winch_out"),
        ("winchout", "winch_out"),
        ("Fuel Delivery - 5 gal", "light_service"),
        ("Out Of Gas", "light_service"),
        ("Lock Out Service", "light_service"),
        # case insensitivity, in every direction
        ("TOW", "tow"),
        ("tow", "tow"),
        ("ToW", "tow"),
        ("HEAVY DUTY TOW - ACCIDENT", "tow"),
        ("jump START", "light_service"),
        ("BATTERY", "light_service"),
        # matches nothing in the shipped rules
        ("Motorcycle Transport", "unclassified"),
        ("Equipment Haul", "unclassified"),
        ("Storage Release", "unclassified"),
        ("Mobile Repair", "unclassified"),
    ],
)
def test_shipped_rules_classify_as_documented(classifier, raw: str, expected: str) -> None:
    assert classifier.classify_service(raw, get_rules()) == expected


def test_classification_is_pure(classifier) -> None:
    """Same input, same answer, and the raw string is never mutated."""
    raw = "Heavy Duty Tow - Accident"
    rules = get_rules()
    first = classifier.classify_service(raw, rules)
    second = classifier.classify_service(raw, rules)
    assert first == second == "tow"
    assert raw == "Heavy Duty Tow - Accident"


# --------------------------------------------------------------------------
# first match wins
# --------------------------------------------------------------------------


def test_accident_tow_is_a_tow_because_tow_is_declared_first(classifier) -> None:
    """The comment in rules.yaml says tow sits first deliberately. Prove it."""
    rules = get_rules()
    order = [name for name in rules["service_classes"] if not name.startswith("_")]
    assert order[0] == "tow", "tow must be the first service class in rules.yaml"
    # "Accident Recovery" contains both `accident` (tow) and nothing else, and
    # "Winch Out After Accident" contains `winch out` *and* `accident`.
    assert classifier.classify_service("Winch Out After Accident", rules) == "tow"


def test_reordering_the_rules_reorders_precedence(classifier) -> None:
    """Move a block up and the same string classifies differently.

    This is the property that makes rules.yaml genuinely declarative: precedence
    lives in the file, not in the code.
    """
    base = get_rules()
    ambiguous = "Winch Out After Accident"

    tow_first = _rules_with_classes(
        {
            "tow": {"match_any": ["tow", "accident"]},
            "winch_out": {"match_any": ["winch out"]},
            "_default": "unclassified",
        },
        base,
    )
    winch_first = _rules_with_classes(
        {
            "winch_out": {"match_any": ["winch out"]},
            "tow": {"match_any": ["tow", "accident"]},
            "_default": "unclassified",
        },
        base,
    )

    assert classifier.classify_service(ambiguous, tow_first) == "tow"
    assert classifier.classify_service(ambiguous, winch_first) == "winch_out"


def test_first_matching_entry_within_a_class_still_wins(classifier) -> None:
    base = get_rules()
    rules = _rules_with_classes(
        {
            "specific": {"match_any": ["heavy duty tow"]},
            "general": {"match_any": ["tow"]},
            "_default": "unclassified",
        },
        base,
    )
    assert classifier.classify_service("Heavy Duty Tow", rules) == "specific"
    assert classifier.classify_service("Light Duty Tow", rules) == "general"


# --------------------------------------------------------------------------
# the mandatory default
# --------------------------------------------------------------------------


def test_default_is_used_when_nothing_matches(classifier) -> None:
    base = get_rules()
    rules = _rules_with_classes(
        {"tow": {"match_any": ["tow"]}, "_default": "unclassified"}, base
    )
    assert classifier.classify_service("Motorcycle Transport", rules) == "unclassified"


def test_unclassified_is_the_fallback_even_without_a_default_key(classifier) -> None:
    """``_default`` is mandatory, so a file missing it is a config bug -- but the
    classifier must still return a usable class rather than None or a crash."""
    base = get_rules()
    rules = _rules_with_classes({"tow": {"match_any": ["tow"]}}, base)
    assert classifier.classify_service("Motorcycle Transport", rules) == "unclassified"


def test_a_custom_default_is_honoured(classifier) -> None:
    base = get_rules()
    rules = _rules_with_classes(
        {"tow": {"match_any": ["tow"]}, "_default": "needs_review"}, base
    )
    assert classifier.classify_service("Motorcycle Transport", rules) == "needs_review"


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_empty_service_type_does_not_blow_up(classifier, raw) -> None:
    assert classifier.classify_service(raw, get_rules()) == "unclassified"


def test_underscore_keys_are_directives_not_service_classes(classifier) -> None:
    """A key starting with ``_`` must never be returned as a class name."""
    base = get_rules()
    rules = _rules_with_classes(
        {
            "_default": "unclassified",
            "_note": {"match_any": ["tow"]},
            "tow": {"match_any": ["tow"]},
        },
        base,
    )
    assert classifier.classify_service("Flatbed Tow", rules) == "tow"


# --------------------------------------------------------------------------
# hot reload -- no restart, no redeploy
# --------------------------------------------------------------------------


def test_editing_rules_yaml_changes_classification_without_a_restart(
    classifier, write_config
) -> None:
    raw = "Motorcycle Transport"

    before_rules = get_rules()
    before_version = rules_version()
    assert classifier.classify_service(raw, before_rules) == "unclassified"

    # An operator adds a service class. No code change, no redeploy.
    updated = {
        "version": 2,
        "service_classes": {
            "moto_transport": {"match_any": ["motorcycle", "moto transport"], "accept": True},
            "tow": {"match_any": ["tow", "recovery", "accident", "flatbed"]},
            "_default": "unclassified",
        },
        "acceptance_policy": before_rules.get("acceptance_policy", {}),
        "alerts": before_rules.get("alerts", []),
        "denial_reason_normalization": [],
    }
    write_config("rules", updated)

    after_rules = get_rules()
    assert after_rules is not before_rules, "the loader served a stale cached copy"
    assert classifier.classify_service(raw, after_rules) == "moto_transport"
    # Still a tow, because the tow block survived the edit.
    assert classifier.classify_service("Flatbed Tow", after_rules) == "tow"

    after_version = rules_version()
    assert after_version != before_version, "rules_version must change when rules.yaml changes"
    assert after_version.startswith("2-"), f"version key not picked up: {after_version}"


def test_hot_reload_also_picks_up_a_removed_service_class(classifier, write_config) -> None:
    write_config(
        "rules",
        {
            "version": 3,
            "service_classes": {"_default": "unclassified"},
            "acceptance_policy": {},
            "alerts": [],
        },
    )
    assert classifier.classify_service("Flatbed Tow", get_rules()) == "unclassified"


# --------------------------------------------------------------------------
# acceptance policy
# --------------------------------------------------------------------------


def test_policy_flags_follow_the_acceptance_policy(classifier) -> None:
    rules = get_rules()

    tow = classifier.policy_flags(
        {"service_class": "tow", "status": "accepted", "service_type_raw": "Flatbed Tow"}, rules
    )
    assert isinstance(tow, dict)
    assert _flag(tow, "should_accept") is True
    assert _flag(tow, "should_decline") is False

    winch = classifier.policy_flags({"service_class": "winch_out", "status": "denied"}, rules)
    assert _flag(winch, "should_accept") is True

    light = classifier.policy_flags({"service_class": "light_service", "status": "denied"}, rules)
    assert _flag(light, "should_accept") is False
    assert _flag(light, "should_decline") is True


def test_policy_flags_mark_a_light_service_job_accepted_against_policy(classifier) -> None:
    """Accepting light service is the policy violation the reports surface."""
    flags = classifier.policy_flags(
        {"service_class": "light_service", "status": "accepted"}, get_rules()
    )
    violation = _flag(
        flags,
        "policy_violation",
        "violates_policy",
        "against_policy",
        "accepted_against_policy",
        "unexpected_acceptance",
    )
    assert violation is True, (
        "policy_flags must flag light service that was accepted; "
        f"got keys {sorted(flags)}"
    )


def test_policy_flags_mark_a_missed_tow(classifier) -> None:
    flags = classifier.policy_flags({"service_class": "tow", "status": "denied"}, get_rules())
    missed = _flag(
        flags,
        "missed_opportunity",
        "missed_tow",
        "policy_violation",
        "violates_policy",
        "should_have_accepted",
    )
    assert missed is True, (
        "policy_flags must flag a tow that was declined; " f"got keys {sorted(flags)}"
    )


def test_policy_flags_are_neutral_for_an_unclassified_row(classifier) -> None:
    flags = classifier.policy_flags(
        {"service_class": "unclassified", "status": "denied"}, get_rules()
    )
    assert isinstance(flags, dict)
    assert _flag(flags, "should_accept", default=False) is False
    assert _flag(flags, "should_decline", default=False) is False


# --------------------------------------------------------------------------
# alert evaluation
# --------------------------------------------------------------------------


def test_missed_tow_alert_fires_for_a_denied_tow(classifier) -> None:
    fired = classifier.evaluate_alerts(
        {"service_class": "tow", "status": "denied"}, get_rules()
    )
    assert "missed_tow" in _alert_ids(fired)


def test_missed_tow_alert_does_not_fire_for_an_accepted_tow(classifier) -> None:
    fired = classifier.evaluate_alerts(
        {"service_class": "tow", "status": "accepted"}, get_rules()
    )
    assert "missed_tow" not in _alert_ids(fired)


def test_unclassified_alert_fires(classifier) -> None:
    fired = classifier.evaluate_alerts(
        {"service_class": "unclassified", "status": "denied"}, get_rules()
    )
    assert "unclassified_service_type" in _alert_ids(fired)


def test_client_acceptance_drop_needs_both_conditions(classifier) -> None:
    rules = get_rules()

    fired = classifier.evaluate_alerts(
        {"client_acceptance_rate_24h": 0.31, "client_offers_24h": 14}, rules
    )
    assert "client_acceptance_drop" in _alert_ids(fired)

    # Same bad rate, not enough offers to be meaningful.
    quiet = classifier.evaluate_alerts(
        {"client_acceptance_rate_24h": 0.31, "client_offers_24h": 4}, rules
    )
    assert "client_acceptance_drop" not in _alert_ids(quiet)


def test_a_rule_referencing_an_absent_name_simply_does_not_fire(classifier) -> None:
    """rules.yaml says so explicitly, and a missing metric must never raise."""
    fired = classifier.evaluate_alerts({"service_class": "tow", "status": "accepted"}, get_rules())
    assert "client_acceptance_drop" not in _alert_ids(fired)


def test_alerts_carry_their_severity(classifier) -> None:
    fired = classifier.evaluate_alerts({"service_class": "unclassified"}, get_rules())
    matches = [alert for alert in fired if _alert_id(alert) == "unclassified_service_type"]
    assert matches, "unclassified_service_type did not fire"
    alert = matches[0]
    severity = alert.get("severity") if isinstance(alert, dict) else getattr(alert, "severity", None)
    assert severity == "low"


def test_a_new_alert_added_to_yaml_fires_without_a_code_change(classifier, write_config) -> None:
    base = get_rules()
    write_config(
        "rules",
        {
            "version": 4,
            "service_classes": base["service_classes"],
            "acceptance_policy": base.get("acceptance_policy", {}),
            "alerts": [
                {
                    "id": "big_money_declined",
                    "when": "status == 'denied' and amount > 500",
                    "severity": "high",
                }
            ],
        },
    )
    fired = classifier.evaluate_alerts({"status": "denied", "amount": 900}, get_rules())
    assert _alert_ids(fired) == ["big_money_declined"]

    quiet = classifier.evaluate_alerts({"status": "denied", "amount": 100}, get_rules())
    assert _alert_ids(quiet) == []


def test_an_unsafe_alert_expression_does_not_execute(classifier, write_config) -> None:
    """A malicious or broken ``when:`` must be inert, not a code path."""
    base = get_rules()
    write_config(
        "rules",
        {
            "version": 5,
            "service_classes": base["service_classes"],
            "acceptance_policy": base.get("acceptance_policy", {}),
            "alerts": [
                {"id": "evil", "when": "__import__('os').system('echo pwned')", "severity": "high"},
                {"id": "fine", "when": "status == 'denied'", "severity": "low"},
            ],
        },
    )
    fired = classifier.evaluate_alerts({"status": "denied"}, get_rules())
    ids = _alert_ids(fired)
    assert "evil" not in ids
    assert "fine" in ids
