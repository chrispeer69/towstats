"""The maps views: ZIP-centroid geocoding, the two maps, and the $35 tally.

The dashboard route sweep in test_web.py already proves /maps and /api/maps
return 200 on an empty datastore and on the fixture. This module asserts the
*content*: that offers aggregate onto ZIP centroids, that the not-accepted set is
exactly the missed buckets with the right popup fields, and that the
light-service dollar tally counts what it says it counts.

The fixture uses Michigan addresses with no ZIP, so it cannot exercise the map
placement. These tests therefore insert Request rows directly with real central
Ohio ZIPs -- the same shape the JSON API produces in production, where
``pickup_zip`` is populated on 3,122 of 3,124 records.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from towbook_agent.core.db import get_session
from towbook_agent.core.models import Request, client_key_for
from towbook_agent.web import geo
from towbook_agent.web import queries as q


# --------------------------------------------------------------------------
# Direct row insertion -- the API-shaped record the CSV fixture cannot make
# --------------------------------------------------------------------------


def _add(
    rid: str,
    *,
    zip_code: str | None,
    service_class: str = "tow",
    service_type: str = "Standard Tow",
    status: str = "expired",
    status_code: int | None = 5,
    status_raw: str = "Expired",
    offered: datetime | None = None,
    driver: str = "",
    reason: str = "",
    client: str = "Agero (Swoop)",
    address: str = "100 High St, Columbus OH",
) -> None:
    """Insert one request row and commit it, so a later read sees it."""
    offered = offered or datetime(2026, 7, 20, 14, 0, 0)
    with get_session() as session:
        session.add(
            Request(
                request_id=rid,
                company_id="default",
                client_name=client,
                client_key=client_key_for(client),
                offered_at=offered,
                status=status,
                status_raw=status_raw,
                status_code=status_code,
                denial_reason=reason or None,
                service_type_raw=service_type,
                service_class=service_class,
                pickup_location=address,
                pickup_zip=zip_code,
                driver_assigned=driver or None,
            )
        )


ANCHOR = date(2026, 7, 20)


# --------------------------------------------------------------------------
# geo module
# --------------------------------------------------------------------------


def test_normalize_zip_accepts_the_shapes_the_feed_uses():
    assert geo.normalize_zip("43215") == "43215"
    assert geo.normalize_zip("43215-1234") == "43215"
    assert geo.normalize_zip(43215) == "43215"      # a numeric cell
    assert geo.normalize_zip("43215.0") == "43215"  # some exports render floats
    assert geo.normalize_zip("") is None
    assert geo.normalize_zip(None) is None
    assert geo.normalize_zip("nope") is None


def test_zip_from_text_is_a_fallback_only():
    assert geo.zip_from_text("100 High St, Columbus, OH 43215") == "43215"
    assert geo.zip_from_text("Columbus OH") is None


def test_centroid_lookup_resolves_a_known_columbus_zip():
    latlng = geo.centroid_for_zip("43215")  # downtown Columbus
    assert latlng is not None
    lat, lng = latlng
    assert 39.5 < lat < 40.5
    assert -83.5 < lng < -82.5
    # Out of state / unknown ZIP is not invented.
    assert geo.centroid_for_zip("99999") is None
    assert geo.centroid_count() > 1000


def test_place_is_deterministic_and_spreads_a_cluster():
    # A lone marker sits exactly on the centroid.
    assert geo.place(40.0, -83.0, 0, 1) == (40.0, -83.0)
    # A cluster spreads, deterministically, and stays near the centroid.
    a = geo.place(40.0, -83.0, 0, 5)
    b = geo.place(40.0, -83.0, 1, 5)
    assert a != b
    assert geo.place(40.0, -83.0, 1, 5) == b  # same inputs -> same point
    assert abs(a[0] - 40.0) < 0.01 and abs(a[1] + 83.0) < 0.02


# --------------------------------------------------------------------------
# Map 1 -- offered heat
# --------------------------------------------------------------------------


def test_offered_heat_aggregates_by_zip_and_counts_unmapped():
    _add("o1", zip_code="43215", status="accepted", status_code=1, status_raw="Accepted")
    _add("o2", zip_code="43215")
    _add("o3", zip_code="43017")
    _add("o4", zip_code="99999")  # valid 5 digits, but not an Ohio centroid
    _add("o5", zip_code=None, address="Somewhere with no zip")

    snap = q.maps_snapshot(scope="day", anchor=ANCHOR)
    offered = snap["offered"]

    assert offered["total"] == 5
    assert offered["unmapped"] == 2          # 99999 and the ZIP-less row
    assert offered["mapped"] == 3
    assert offered["zip_count"] == 2         # 43215 and 43017
    assert offered["max_weight"] == 2        # two offers in 43215

    by_zip = {z["zip"]: z for z in offered["zones"]}
    assert by_zip["43215"]["offers"] == 2
    assert by_zip["43215"]["accepted"] == 1
    assert by_zip["43017"]["offers"] == 1
    # Every heat point is [lat, lng, weight].
    assert all(len(point) == 3 for point in offered["points"])


# --------------------------------------------------------------------------
# Map 2 -- declined / not accepted
# --------------------------------------------------------------------------


def test_declined_map_holds_only_not_accepted_jobs_with_full_popup_fields():
    # Won and in-flight must NOT appear on the declined map.
    _add("won", zip_code="43215", status="accepted", status_code=1, status_raw="Accepted")
    _add("inflight", zip_code="43215", status="pending", status_code=6, status_raw="Accepting")
    # A job we actively declined, with a reason and a person who actioned it.
    _add(
        "decl",
        zip_code="43017",
        status="denied",
        status_code=2,
        status_raw="Rejected",
        driver="J. Whitfield",
        reason="No trucks available",
        address="55 Bridge St, Dublin OH",
    )
    # A job nobody answered.
    _add("noresp", zip_code="43004", status="expired", status_code=5, status_raw="Expired")
    # A client withdrawal.
    _add("withdrew", zip_code="43004", status="canceled", status_code=40, status_raw="Rejected By Motor Club")

    snap = q.maps_snapshot(scope="day", anchor=ANCHOR)
    declines = snap["declines"]

    assert declines["total"] == 3  # decl + noresp + withdrew; not won/in-flight
    assert declines["by_bucket"]["declined"] == 1
    assert declines["by_bucket"]["no_response"] == 1
    assert declines["by_bucket"]["client_withdrew"] == 1
    assert declines["mapped"] == 3

    markers = {mk["ref"]: mk for mk in declines["markers"]}
    assert set(markers) == {"decl", "noresp", "withdrew"}

    declined = markers["decl"]
    assert declined["outcome"] == "declined"
    assert declined["responded"] is True
    assert declined["responded_by"] == "J. Whitfield"
    assert declined["reason"] == "No trucks available"
    assert declined["address"] == "55 Bridge St, Dublin OH"
    assert declined["service_type"] == "Standard Tow"

    # Nobody answered -> responded reads False, which is the honest signal.
    assert markers["noresp"]["responded"] is False
    assert markers["noresp"]["responded_by"] == ""


# --------------------------------------------------------------------------
# The $35 light-service tally
# --------------------------------------------------------------------------


def test_light_service_tally_multiplies_declined_light_jobs_by_the_flat_value():
    # Three declined light-service jobs, two of one type and one of another.
    _add("l1", zip_code="43215", service_class="light_service", service_type="Tire Change",
         status="denied", status_code=2, status_raw="Rejected")
    _add("l2", zip_code="43017", service_class="light_service", service_type="Tire Change",
         status="expired", status_code=5, status_raw="Expired")
    _add("l3", zip_code="43004", service_class="light_service", service_type="Lock Out",
         status="denied", status_code=2, status_raw="Rejected")
    # A declined TOW must never be priced by the light-service figure.
    _add("t1", zip_code="43215", service_class="tow", status="denied", status_code=2, status_raw="Rejected")
    # An ACCEPTED light job is not "given away".
    _add("l_ok", zip_code="43215", service_class="light_service", service_type="Jump Start",
         status="accepted", status_code=1, status_raw="Accepted")

    snap = q.maps_snapshot(scope="day", anchor=ANCHOR)
    light = snap["light_service"]

    assert light["unit_value"] == 35
    assert light["count"] == 3               # the three declined light jobs only
    assert light["total_value"] == 105.0     # 3 x $35
    by_type = {row["service_type"]: row for row in light["by_type"]}
    assert by_type["Tire Change"]["count"] == 2
    assert by_type["Tire Change"]["value"] == 70.0
    assert by_type["Lock Out"]["count"] == 1


def test_light_service_value_is_config_driven(write_config):
    _add("l1", zip_code="43215", service_class="light_service", service_type="Tire Change",
         status="denied", status_code=2, status_raw="Rejected")

    import yaml
    from conftest import SHIPPED_CONFIG_DIR

    rules = yaml.safe_load((SHIPPED_CONFIG_DIR / "rules.yaml").read_text(encoding="utf-8"))
    rules["missed_work"]["light_service_value"] = 50
    write_config("rules", rules)

    snap = q.maps_snapshot(scope="day", anchor=ANCHOR)
    assert snap["light_service"]["unit_value"] == 50
    assert snap["light_service"]["total_value"] == 50.0


# --------------------------------------------------------------------------
# Daily / weekly / monthly review windows
# --------------------------------------------------------------------------


def test_scope_windows_select_the_right_days():
    # One offer on three different days of the same week/month.
    _add("d1", zip_code="43215", offered=datetime(2026, 7, 20, 9, 0))   # Mon
    _add("d2", zip_code="43215", offered=datetime(2026, 7, 22, 9, 0))   # Wed
    _add("d3", zip_code="43215", offered=datetime(2026, 7, 28, 9, 0))   # next Tue
    _add("d4", zip_code="43215", offered=datetime(2026, 6, 15, 9, 0))   # previous month

    day = q.maps_snapshot(scope="day", anchor=date(2026, 7, 20))
    assert day["offered"]["total"] == 1

    week = q.maps_snapshot(scope="week", anchor=date(2026, 7, 20))
    # Mon Jul 20 .. Sun Jul 26 -> d1 and d2 only.
    assert week["first_day"] == date(2026, 7, 20)
    assert week["last_day"] == date(2026, 7, 26)
    assert week["offered"]["total"] == 2

    month = q.maps_snapshot(scope="month", anchor=date(2026, 7, 15))
    assert month["first_day"] == date(2026, 7, 1)
    assert month["last_day"] == date(2026, 7, 31)
    assert month["offered"]["total"] == 3  # the three July offers, not June


def test_scope_defaults_to_day_and_labels_the_window():
    snap = q.maps_snapshot(anchor=ANCHOR)
    assert snap["scope"] == "day"
    assert snap["scopes"] == ["day", "week", "month"]
    assert "2026" in snap["label"]
    # prev/next carry the same scope and an ISO date the route can parse.
    assert snap["prev"]["scope"] == "day"
    assert snap["prev"]["date"] == "2026-07-19"
    assert snap["next"]["date"] == "2026-07-21"
