"""The shipped rules, run against the REAL service-type strings.

Every string in :data:`REAL_SERVICE_TYPES` was observed on the owner's actual
account and is transcribed verbatim from ``TOWBOOK_PORTAL_FACTS.md`` section 6.
Nothing here is invented, and nothing is normalised on the way in -- these are
the exact bytes ``serviceNeeded`` carries.

Why this file exists separately from test_classifier.py
-------------------------------------------------------
test_classifier.py proves the *mechanism*: substring matching, file order,
hot reload, the ``_default`` directive. This file proves the *shipped
configuration is correct against the traffic it will actually see*. They fail
for different reasons and a change to one should not be able to mask a
regression in the other.

THE REGRESSION THAT MATTERS MOST
--------------------------------
"Light" in this feed means **light-duty vehicle class, not light service.**
``Light Tow``, ``Light Duty Towing``, ``Light Accident Tow``, ``Light Secondary
Simple Tow`` and ``Light Standard Tow`` are TOWS -- work the owner explicitly
wants -- and they classify as ``tow`` only because ``tow`` is evaluated before
``light_service`` and every one of them contains the substring "tow".

Move ``light_service`` above ``tow`` in rules.yaml and roughly 400 tows a month
silently become "work we do not want": they drop out of the missed-work
inventory, they appear in the close-off report as something to ask a client to
stop sending, and the headline number inverts.
:func:`test_reordering_light_service_above_tow_inverts_the_report` reproduces
that failure deliberately, so the ordering is protected by a test and not only
by a comment.

The two deliberate holes
------------------------
``Start`` and ``Parts Delivery + 1hr Labor`` fall through to ``unclassified``,
and that is the correct answer rather than a gap -- both are genuinely
ambiguous, and the ``unclassified_service_type`` alert exists to put them in
front of the owner. A ``start`` term in ``light_service`` would swallow them
AND be wrong. This file pins the hole open.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from towbook_agent.core.config_loader import CONFIG, get_rules

# --------------------------------------------------------------------------
# The real strings.
#
# Transcribed from TOWBOOK_PORTAL_FACTS.md section 6 (30-day sample, 3,079
# requests). Counts are carried for documentation only -- they weight which
# mistakes are expensive, and no assertion below depends on them.
#
# HONEST NOTE ON THE COUNT. Section 6's heading says "39 distinct" but its
# three-column table renders 36 strings plus an elision ("... + Light Winch"),
# so 37 are actually nameable from that document. The two unnamed strings
# cannot be transcribed without inventing them, and inventing a fixture string
# would make this file assert about traffic that may not exist. So 37 real
# strings are listed, every one of them verbatim, and the missing two are
# recorded here rather than papered over. Section 6's own "gaps found"
# paragraph names Light Winch, Winching and Light Lock-out explicitly, which is
# where the 37th comes from.
#
# CROSS-CHECKED against the live 2-day API archive in raw/2026/07/26/ (189
# records): every one of the 14 distinct strings that pull contained -- Tow,
# Light Tow, Tire Change, Battery Jump, Lock Out, Towing, Light Tire Change,
# Accident Tow (P), Accident Tow, Tow / Flatbed, Flat Bed Towing, Light Duty
# Towing, Start, Medium Duty TOW -- appears below with identical spelling and
# capitalisation.
# --------------------------------------------------------------------------

REAL_SERVICE_TYPES: dict[str, int] = {
    "Tow": 1071,
    "Light Tow": 329,
    "Flat Bed Towing": 82,
    "Tire Change": 76,
    "Battery Jump": 50,
    "Light Duty Towing": 46,
    "Tow / Flatbed": 46,
    "Light Tire Change": 42,
    "Lock Out": 37,
    "Towing": 36,
    "Accident Tow (P)": 33,
    "Light Secondary Simple Tow": 23,
    "Light Lock-out": 21,
    "Accident Tow": 18,
    "Winch Out": 15,
    "Jump Start": 12,
    "Light Start": 12,
    "Light Accident Tow": 7,
    "Fuel Delivery": 7,
    "Flat Tire": 5,
    "Lockout": 4,
    "Flat Bed Accident": 4,
    "Light Fuel Delivery": 4,
    "Medium Duty TOW": 3,
    "Fuel": 3,
    "Winch": 2,
    "Light Duty Unleaded Fuel Delivery": 2,
    "Tire Inflation": 2,
    "Start": 1,
    "Parts Delivery + 1hr Labor": 1,
    "Light Standard Tow": 1,
    "Low Clearance Tow": 1,
    "Heavy Duty TOW": 1,
    "Salvage Tow": 1,
    "Auto Lockout": 1,
    "Winching": 1,
    "Light Winch": 1,
}

#: The two that must stay unclassified. See the module docstring.
DELIBERATELY_UNCLASSIFIED = frozenset({"Start", "Parts Delivery + 1hr Labor"})

#: Every string carrying "Light" that is a TOW, not light service. These are the
#: ones the ordering protects, and the ones a reorder would destroy.
LIGHT_BUT_ACTUALLY_A_TOW = (
    "Light Tow",
    "Light Duty Towing",
    "Light Accident Tow",
    "Light Secondary Simple Tow",
    "Light Standard Tow",
)

#: Strings that legitimately reach light_service: no "tow" in them at all.
GENUINELY_LIGHT_SERVICE = (
    "Tire Change",
    "Light Tire Change",
    "Battery Jump",
    "Lock Out",
    "Lockout",
    "Auto Lockout",
    "Light Lock-out",
    "Jump Start",
    "Light Start",
    "Fuel",
    "Fuel Delivery",
    "Light Fuel Delivery",
    "Light Duty Unleaded Fuel Delivery",
    "Tire Inflation",
    "Flat Tire",
)

#: Winching work. "winch" is the bare stem the shipped rules match on.
WINCH_WORK = ("Winch Out", "Winch", "Winching", "Light Winch")

#: The real denial reasons, verbatim, from TOWBOOK_PORTAL_FACTS.md section 7.
REAL_DENIAL_REASONS: dict[str, int] = {
    "Equipment Not Available": 355,
    "No Drivers Available": 68,
    "Other": 27,
    "Refuse": 23,
    "Out of Service Area": 18,
    "Out Of Coverage Area": 8,
    "Equipment Availability": 7,
    "Not Enough Information": 5,
    "Out of Area": 1,
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def classify_all(classifier: Any) -> dict[str, str]:
    """``{raw string: service_class}`` for every real service type."""
    return {raw: classifier.classify_service(raw) for raw in REAL_SERVICE_TYPES}


def rewrite_rules(write_config, mutate) -> dict[str, Any]:
    """Edit the sandbox rules.yaml through the hot-reloading loader."""
    data = yaml.safe_load((CONFIG.config_dir / "rules.yaml").read_text(encoding="utf-8"))
    mutate(data)
    write_config("rules.yaml", data)
    return get_rules()


# ==========================================================================
# The regression that matters most
# ==========================================================================


@pytest.mark.parametrize("raw", LIGHT_BUT_ACTUALLY_A_TOW)
def test_a_light_duty_tow_is_a_tow_not_light_service(classifier, raw: str) -> None:
    """"Light" is a VEHICLE CLASS here. These are tows and the owner wants them.

    329 Light Tow + 46 Light Duty Towing + 23 Light Secondary Simple Tow + 7
    Light Accident Tow + 1 Light Standard Tow = 406 offers in 30 days. Getting
    this wrong does not produce a slightly worse report -- it moves 406 jobs the
    owner wants into the pile the report recommends closing off.
    """
    assert classifier.classify_service(raw) == "tow"


def test_no_string_containing_tow_ever_reaches_light_service(classifier) -> None:
    """The general form of the rule above, over the whole real vocabulary."""
    wrong = {
        raw: cls
        for raw, cls in classify_all(classifier).items()
        if "tow" in raw.casefold() and cls == "light_service"
    }
    assert wrong == {}, f"these tows were classified as light service: {wrong}"


def solo_matches(classifier, raw: str) -> list[str]:
    """Which service classes would claim ``raw`` if each were the only one.

    Runs the real matching code, one class at a time, so the answer is
    independent of file order. That is what makes it possible to ask whether
    precedence is doing any work at all.
    """
    classes = {
        name: spec
        for name, spec in get_rules()["service_classes"].items()
        if not str(name).startswith("_")
    }
    hits = []
    for name, spec in classes.items():
        only = {"service_classes": {name: spec, "_default": "unclassified"}}
        if classifier.classify_service(raw, only) == name:
            hits.append(name)
    return hits


def put_light_service_first(data: dict[str, Any]) -> None:
    """The reorder rules.yaml warns against."""
    classes = data["service_classes"]
    reordered = {"light_service": classes["light_service"]}
    for name, spec in classes.items():
        if name != "light_service":
            reordered[name] = spec
    data["service_classes"] = reordered


def test_tow_is_declared_before_light_service(classifier) -> None:
    """File order IS evaluation order, so this ordering is a real invariant.

    Asserted directly on the shipped file rather than inferred from behaviour,
    because -- see the next two tests -- the current term lists happen not to
    overlap, so a reorder would not show up in the output today. The ordering is
    the safety net for the term list somebody edits tomorrow.
    """
    names = [
        name
        for name in get_rules()["service_classes"]
        if not str(name).startswith("_")
    ]
    assert names.index("tow") < names.index("light_service")


def test_no_real_service_string_matches_more_than_one_class(classifier) -> None:
    """A finding, pinned: precedence is NOT what protects the Light tows today.

    Running each class's terms in isolation, every real string is claimed by
    exactly one class (or, for the two ambiguous ones, none). "Light Tow"
    reaches ``tow`` because it contains "tow" and matches nothing in
    ``light_service`` at all -- not because ``tow`` is listed first.

    This matters because rules.yaml's comment block says reordering
    ``light_service`` above ``tow`` "would reclassify ~400 tows per month". As
    the file stands that is not true: the reorder is a no-op, as
    :func:`test_reordering_alone_does_not_move_a_single_real_tow` shows. The
    claim only becomes true once a bare "light" term is added, which is exactly
    the plausible edit the warning is really about -- see
    :func:`test_a_bare_light_term_plus_the_reorder_is_what_costs_406_tows`.
    """
    ambiguous = {}
    overlapping = {}
    for raw in REAL_SERVICE_TYPES:
        hits = solo_matches(classifier, raw)
        if len(hits) > 1:
            overlapping[raw] = hits
        elif not hits:
            ambiguous[raw] = hits

    assert overlapping == {}, f"these strings are claimed by two classes: {overlapping}"
    assert set(ambiguous) == set(DELIBERATELY_UNCLASSIFIED)


def test_reordering_alone_does_not_move_a_single_real_tow(
    classifier, write_config
) -> None:
    """The honest version of "do not reorder these".

    Against the 37 real strings the reorder changes nothing, because no Light
    tow matches any light_service term. Asserting the opposite would be a test
    that passes only against a story about the data rather than the data.
    """
    before = classify_all(classifier)
    rewrite_rules(write_config, put_light_service_first)
    after = classify_all(classifier)

    assert after == before
    for raw in LIGHT_BUT_ACTUALLY_A_TOW:
        assert after[raw] == "tow"


def test_a_bare_light_term_plus_the_reorder_is_what_costs_406_tows(
    classifier, write_config
) -> None:
    """The failure the ordering exists to prevent, reproduced exactly.

    Somebody looks at "Light Lock-out", "Light Start" and "Light Fuel Delivery",
    concludes that "light" is the tidy stem for all of them, and adds it. With
    ``tow`` still first, nothing breaks -- the ordering absorbs the mistake.
    Move ``light_service`` up as well and 406 tows per 30 days silently become
    work the close-off report recommends asking clients to stop sending.

    406 is the arithmetic of the real counts: Light Tow 329, Light Duty Towing
    46, Light Secondary Simple Tow 23, Light Accident Tow 7, Light Standard Tow
    1.
    """

    def add_bare_light(data: dict[str, Any]) -> None:
        data["service_classes"]["light_service"]["match_any"].append("light")

    # Step 1: the bad term on its own. The ordering holds the line.
    rewrite_rules(write_config, add_bare_light)
    guarded = classify_all(classifier)
    for raw in LIGHT_BUT_ACTUALLY_A_TOW:
        assert guarded[raw] == "tow", f"{raw} must survive a bad term while tow is first"

    # Step 2: the same term with the ordering removed.
    def add_bare_light_and_reorder(data: dict[str, Any]) -> None:
        add_bare_light(data)
        put_light_service_first(data)

    rewrite_rules(write_config, add_bare_light_and_reorder)
    broken = classify_all(classifier)

    for raw in LIGHT_BUT_ACTUALLY_A_TOW:
        assert broken[raw] == "light_service", raw

    moved = sum(
        count
        for raw, count in REAL_SERVICE_TYPES.items()
        if guarded[raw] == "tow" and broken[raw] == "light_service"
    )
    assert moved == 406, f"expected the documented ~400 tows/month; got {moved}"


# ==========================================================================
# The two deliberate holes
# ==========================================================================


def test_exactly_two_real_service_types_stay_unclassified(classifier) -> None:
    """``Start`` and ``Parts Delivery + 1hr Labor``, and nothing else.

    Both directions are asserted. A NEW unclassified string is a rules gap that
    has to be looked at; one of these two DISAPPEARING means somebody added a
    term that swallowed a genuinely ambiguous job instead of surfacing it.
    """
    unclassified = {
        raw for raw, cls in classify_all(classifier).items() if cls == "unclassified"
    }
    assert unclassified == set(DELIBERATELY_UNCLASSIFIED)


def test_everything_else_is_classified(classifier) -> None:
    """The corrected rules leave 0.2% of real traffic unclassified, not 2.0%."""
    results = classify_all(classifier)
    unresolved = {
        raw: count
        for raw, count in REAL_SERVICE_TYPES.items()
        if results[raw] == "unclassified"
    }
    assert set(unresolved) == set(DELIBERATELY_UNCLASSIFIED)

    total = sum(REAL_SERVICE_TYPES.values())
    assert sum(unresolved.values()) / total < 0.01, (
        "the shipped rules must resolve better than 99% of real traffic by volume"
    )


def test_adding_a_start_term_would_swallow_the_ambiguous_ones(
    classifier, write_config
) -> None:
    """Why `start` is deliberately absent from light_service.

    "Jump Start" and "Light Start" are matched by their full phrases. A bare
    `start` term looks like a tidy-up and is not: it silently absorbs the six
    "Start" rows nobody has decided about, and "Start" is not necessarily light
    service at all.
    """
    assert classifier.classify_service("Start") == "unclassified"

    def add_bare_start(data: dict[str, Any]) -> None:
        data["service_classes"]["light_service"]["match_any"].append("start")

    rewrite_rules(write_config, add_bare_start)

    assert classifier.classify_service("Start") == "light_service", (
        "this is the wrong answer, and it is why the term is not shipped"
    )


# ==========================================================================
# The rest of the real vocabulary
# ==========================================================================


@pytest.mark.parametrize("raw", GENUINELY_LIGHT_SERVICE)
def test_real_light_service_strings_classify_as_light_service(
    classifier, raw: str
) -> None:
    """The only strings that legitimately reach light_service: no "tow" in them.

    Together these are the ~450 offers per 30 days that produced 13 jobs -- the
    population the close-off report is built on.
    """
    assert classifier.classify_service(raw) == "light_service"


@pytest.mark.parametrize("raw", WINCH_WORK)
def test_the_bare_winch_stem_covers_every_real_winching_string(
    classifier, raw: str
) -> None:
    """Three long forms were replaced by the stem. `Light Winch` is the proof it
    was needed: it is winching on a light-duty vehicle and matched none of them.
    """
    assert classifier.classify_service(raw) == "winch_out"


def test_the_spaced_and_unspaced_flatbed_spellings_both_reach_tow(
    classifier,
) -> None:
    """`flatbed` alone did not match "Flat Bed Towing" -- 86 offers in 30 days."""
    assert classifier.classify_service("Flat Bed Towing") == "tow"
    assert classifier.classify_service("Tow / Flatbed") == "tow"
    assert classifier.classify_service("Flat Bed Accident") == "tow"


def test_capitalisation_is_not_significant(classifier) -> None:
    """The portal writes "Medium Duty TOW" and "Heavy Duty TOW" in caps."""
    assert classifier.classify_service("Medium Duty TOW") == "tow"
    assert classifier.classify_service("Heavy Duty TOW") == "tow"


def test_every_real_string_gets_a_name_from_the_configured_vocabulary(
    classifier,
) -> None:
    """No string may produce a class the rules do not declare."""
    declared = set(classifier.service_class_names())
    assert set(classify_all(classifier).values()) <= declared


def test_the_verbatim_string_is_never_mutated(classifier) -> None:
    """Hard constraint: service_type_raw is read, never rewritten. Classifying
    must not depend on -- or produce -- a cleaned-up copy."""
    for raw in REAL_SERVICE_TYPES:
        copy = str(raw)
        classifier.classify_service(raw)
        assert raw == copy


# ==========================================================================
# Denial reasons -- the other half of the real vocabulary
# ==========================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Equipment Not Available", "equipment_unavailable"),
        ("Equipment Availability", "equipment_unavailable"),
        ("No Drivers Available", "no_drivers"),
        ("Out of Service Area", "out_of_area"),
        ("Out Of Coverage Area", "out_of_area"),
        ("Out of Area", "out_of_area"),
        ("Other", "other"),
        ("Refuse", "other"),
        ("Not Enough Information", "other"),
    ],
)
def test_every_real_denial_reason_normalizes(
    classifier, raw: str, expected: str
) -> None:
    """All nine real strings, with their verified groupings.

    The duplicates are the point: "Equipment Not Available" (355) and
    "Equipment Availability" (7) are the same cause typed two ways, and counting
    them separately splits the single biggest decline reason in the account.
    """
    assert raw in REAL_DENIAL_REASONS
    assert classifier.normalize_denial_reason(raw) == expected


def test_the_broad_other_bucket_never_steals_a_specific_match(classifier) -> None:
    """`other` is last in the file on purpose. Its terms are the broadest, and a
    bucket meaning "we did not record a real reason" must not win a match a
    specific bucket could have taken."""
    # "Not Enough Information" contains no "other"/"refuse" substring, so this
    # is really a check that ORDER did not get shuffled in a way that lets the
    # catch-all absorb a reason with its own remedy.
    assert classifier.normalize_denial_reason("Not Enough Information") == "other"
    assert classifier.normalize_denial_reason("Equipment Not Available") == (
        "equipment_unavailable"
    )


def test_an_unseen_denial_reason_is_not_forced_into_a_bucket(classifier) -> None:
    """Same principle as `_default: unclassified`: a new reason must surface."""
    assert classifier.normalize_denial_reason("Truck Caught Fire") not in {
        "equipment_unavailable",
        "no_drivers",
        "out_of_area",
    }
