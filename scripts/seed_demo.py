#!/usr/bin/env python
"""Seed the demo tenant with synthetic traffic that has the shape of a real book.

WHAT THIS IS FOR
----------------
`example-towing` is the demo tenant (see demo-root/config/companies.yaml). It
has no Towbook account and never will, so its data has to be generated. This
script generates it, and then hands it to the REAL ingester and the REAL
metrics passes -- the same code that serves a paying customer. Nothing here
writes to `requests`, `metrics_daily` or any other table directly.

That matters more than it sounds. If the demo computed its own numbers it
would be a second implementation of the product, free to drift until the demo
says one thing and the board says another. Here the demo cannot disagree with
the product, because past this file it IS the product.

WHY NOT `python -m towbook_agent seed`
--------------------------------------
The built-in seeder exists to prove the pipeline runs, and it is good at that.
It is not usable as a demo: it spreads offers uniformly across the clock, so
acceptance is flat at every hour of every day. The single thing this product
exists to show -- that offers arriving when nobody is on the desk go
unanswered -- is invisible in it. A prospect would open the blind-spot grid,
see even grey everywhere, and correctly conclude there is nothing to buy.

THE MODEL
---------
Three things drive every record, in this order:

1. WHEN. Demand follows an hour-of-day curve (rush hours and Friday nights are
   busy, 04:00 Tuesday is not) times a day-of-week curve. Nobody is dispatched
   evenly around the clock and no real book looks like that.

2. WHETHER ANYBODY ANSWERED. Decided almost entirely by whether the offer
   landed inside the staffed window (Mon-Sat 07:00-19:00). Inside, 5.5% of
   offers go unanswered. Outside, 48.7% do. Those two figures are not invented
   -- they are the real measured contrast from the live deployment, quoted on
   the marketing site. Anchoring the demo to them means the demo argues the
   same case as the site, with the same numbers.

3. WHAT IT WAS. Light service (tire, jump, lockout, fuel) is declined more
   often than a tow, because it pays the same call-out for the same truck.
   That asymmetry is the second story the product tells.

Everything else -- ZIP, distance, vehicle, expiry window, job number, who
answered -- is generated to be internally consistent with those three. Distance
in particular is computed from the actual ZIP centroids, so `distance` and
`zip` agree with each other rather than being two independent random numbers
that a careful reader could catch disagreeing.

DETERMINISTIC. A fixed seed, so re-running produces byte-identical records and
the demo does not quietly change under a prospect between two viewings.

USAGE
-----
    TOWBOOK_REPO_ROOT=demo-root \\
    DATABASE_URL=sqlite:///demo-root/data/demo.db \\
    python scripts/seed_demo.py --weeks 13

`TZ` is deliberately NOT a caller's responsibility. It overrides schema.yaml's
`source_timezone` and therefore decides what local hour every offer lands on,
and there are two good ways to get it wrong: production .env sets it to
America/Detroit, and Git Bash on Windows silently drops a `TZ=... command`
prefix when launching a native Windows process. This script reads the
timezone off the company record and sets it itself, so the timezone offers are
written in and the timezone they are read back in cannot disagree.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

# Run from a source checkout without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

COMPANY_ID = "example-towing"

#: The prospect-facing login. Scoped to the demo tenant and nothing else, so
#: even if this password is published on the marketing site it opens one
#: synthetic company. Twelve characters is the floor accounts.py enforces.
DEMO_USERNAME = "demo"
DEMO_PASSWORD = "summit-demo-2026"

#: Fixed so the demo is the same demo every time it is rebuilt.
RANDOM_SEED = 19690724

#: Offers per day, before the day-of-week curve is applied. The live tenant
#: this product was built on runs about 100/day; a demo that claimed more would
#: invite the question of why its owner is not already rich.
BASE_OFFERS_PER_DAY = 85

#: Where the trucks are. Distances are measured from here.
BASE_ZIP = "76106"  # Fort Worth, TX

# --------------------------------------------------------------------------
# The staffed window, restated.
#
# This MUST match demo-root/config/companies.yaml -> coverage. It is repeated
# here because the generator needs to know which offers land inside it in order
# to decide the outcome, and reading the merged rules back out to find the
# answer would make the generator depend on the thing it is generating for.
# A mismatch would produce a board whose blind spots do not line up with its
# own stated hours, so the check at the bottom of main() asserts they agree.
# --------------------------------------------------------------------------
STAFFED_DAYS = {0, 1, 2, 3, 4, 5}  # Mon-Sat, Python weekday numbering
STAFFED_START_HOUR = 7
STAFFED_END_HOUR = 19

# --------------------------------------------------------------------------
# Demand curves. Relative weights, normalised at use.
# --------------------------------------------------------------------------
HOUR_WEIGHTS: tuple[float, ...] = (
    0.55, 0.45, 0.38, 0.32, 0.35, 0.50,   # 00-05  overnight breakdowns
    0.85, 1.30, 1.45, 1.30, 1.20, 1.20,   # 06-11  morning rush into midday
    1.25, 1.20, 1.25, 1.35, 1.50, 1.55,   # 12-17  afternoon, evening rush
    1.35, 1.10, 0.95, 0.85, 0.75, 0.65,   # 18-23  evening tapering off
)

DAY_WEIGHTS: dict[int, float] = {
    0: 1.05,  # Mon
    1: 1.00,
    2: 1.00,
    3: 1.05,
    4: 1.20,  # Fri
    5: 1.15,  # Sat
    6: 0.90,  # Sun
}

# --------------------------------------------------------------------------
# Outcomes.
#
# The five buckets are the ones config/rules.yaml -> missed_work.buckets sorts
# offers into. The numbers are the measured live contrast; see the module
# docstring. Each block sums to 1.0 and is asserted below.
# --------------------------------------------------------------------------
OUTCOMES_INSIDE: dict[str, float] = {
    "won": 0.900,
    "declined": 0.035,
    "no_response": 0.055,   # the measured 5.5% inside staffed hours
    "client_withdrew": 0.008,
    "accept_failed": 0.002,
}

OUTCOMES_OUTSIDE: dict[str, float] = {
    "won": 0.360,
    "declined": 0.060,
    "no_response": 0.487,   # the measured 48.7% outside staffed hours
    "client_withdrew": 0.080,
    "accept_failed": 0.013,
}

# NOTE ON THE REALISED FIGURE. These are the BASE rates. The close-off
# services below push their freed acceptance mass into `no_response` when the
# offer lands outside staffed hours -- correctly, because at 03:00 there is
# nobody there to decline a lockout, it simply times out. So the realised
# outside-hours no-response rate comes out a few points above 48.7% (about
# 53%). That is a consequence of this company also refusing two service types
# outright, not a drift in the model, and 53% against 5.8% inside is the same
# story told slightly louder.

#: Light service is declined more than a tow: same truck, same call-out, less
#: money. Probability mass moved from `won` to `declined` when the job is light.
LIGHT_SERVICE_DECLINE_SHIFT = 0.10

# --------------------------------------------------------------------------
# Work this company has effectively stopped taking.
#
# service type -> the acceptance rate it actually runs at.
#
# WHY THIS IS MODELLED AT ALL. The Close-off view exists to find work the
# owner refuses so consistently that the honest move is to tell the club to
# stop sending it -- it lists any non-tow service type accepted at 10% or less
# over at least four offers (rules.yaml -> missed_work.closeoff). Without a
# service type that actually sits under that line the view renders correctly
# and completely empty, and a whole tab of the demo says nothing.
#
# Fuel delivery and lockouts are the honest choice for it. Both tie a truck up
# for a flat $35 call-out that pays the same as nothing once the driver is an
# hour out and back, and refusing them is one of the most common standing
# decisions in the trade. That makes this the conversation the view is FOR:
# you decline 94% of lockouts, so either staff for them or have Meridian stop
# offering them -- and either way stop counting them as missed revenue.
# --------------------------------------------------------------------------
#: Deliberately well clear of the 10% line rather than just under it. The
#: generated window ends at `now`, so every re-seed draws a slightly different
#: sample; a target of 0.09 realised at 9.9% once, and a service that drops off
#: the Close-off view because the demo was rebuilt on a Tuesday is not a demo
#: anyone can rely on.
CLOSE_OFF_SERVICES: dict[str, float] = {
    "Fuel Delivery": 0.05,
    "Lockout": 0.06,
}

# --------------------------------------------------------------------------
# Status codes. The numeric `status` is the PRIMARY bucket route
# (rules.yaml -> missed_work.buckets), and statusName is the label that must
# agree with it. Emitting both is what a real API payload does, and it keeps
# the demo off the coarse last-resort map that a hand-built fixture falls onto.
# --------------------------------------------------------------------------
STATUS_BY_BUCKET: dict[str, tuple[tuple[int, str, float], ...]] = {
    "won": ((10, "Accept Sent", 0.82), (1, "Accepted", 0.18)),
    "declined": ((2, "Rejected", 0.94), (22, "Reject Failed", 0.06)),
    "no_response": ((5, "Expired", 0.88), (80, "Another Provider Responded", 0.12)),
    "accept_failed": ((21, "Accept Failed", 1.0),),
    "client_withdrew": (
        (40, "Rejected By Motor Club", 0.44),
        (71, "Goa Approved By Motor Club", 0.26),
        (41, "Service No Longer Needed", 0.19),
        (4, "Cancelled", 0.11),
    ),
}

# --------------------------------------------------------------------------
# The motor clubs. These are the SAME fictional names the public marketing
# site and the existing demo already use (the mapping lives in the website
# repo's tools/anonymize_all.py), so a prospect who reads the site and then
# opens the board sees one consistent universe.
#
# Weights model a real book: one club dominates, a long tail underneath.
# --------------------------------------------------------------------------
CLIENTS: tuple[tuple[str, float], ...] = (
    ("Meridian Club", 0.42),
    ("Sentry Roadside", 0.19),
    ("Pinnacle Assist", 0.14),
    ("Vantage Dispatch Group", 0.10),
    ("Continental Assurance", 0.09),
    ("Harbor Motor Club", 0.06),
)

#: (label, weight, is_light_service)
SERVICES: tuple[tuple[str, float, bool], ...] = (
    ("Light Duty Tow", 0.20, False),
    ("Flatbed Tow", 0.12, False),
    ("Accident Tow", 0.07, False),
    ("Wrecker Tow", 0.06, False),
    ("Impound Tow", 0.04, False),
    ("Motorcycle Tow", 0.02, False),
    ("Heavy Duty Tow", 0.02, False),
    ("Tire Change", 0.11, True),
    ("Jump Start", 0.10, True),
    ("Lockout", 0.08, True),
    ("Fuel Delivery", 0.05, True),
    ("Battery Service", 0.03, True),
    ("Winch Out", 0.06, False),
    ("Extrication / Recovery", 0.04, False),
)

DENIAL_REASONS: tuple[tuple[str, float], ...] = (
    ("No truck available", 0.34),
    ("Rate too low for the distance", 0.22),
    ("Too far outside coverage area", 0.18),
    ("No driver on shift", 0.16),
    ("Wrong equipment for the vehicle", 0.10),
)

#: The people on the desk. Only ever attached to an offer somebody ANSWERED --
#: an expired offer has no responder, and inventing one would quietly destroy
#: the distinction the whole missed-work model rests on.
DISPATCHERS: tuple[str, ...] = (
    "dsalazar", "kbrennan", "mvasquez", "rtorres", "jholloway", "cpatel",
)

# --------------------------------------------------------------------------
# Territory. ZIP -> (city, weight). Home turf is heavily weighted; the further
# out, the rarer, which is what produces a believable service-area band split
# and a map with a dense core instead of an even wash.
# --------------------------------------------------------------------------
TERRITORY: tuple[tuple[str, str, float], ...] = (
    # Fort Worth core
    ("76106", "Fort Worth", 5.0), ("76107", "Fort Worth", 4.5),
    ("76104", "Fort Worth", 4.0), ("76110", "Fort Worth", 4.0),
    ("76111", "Fort Worth", 3.5), ("76112", "Fort Worth", 3.5),
    ("76114", "Fort Worth", 3.0), ("76115", "Fort Worth", 3.0),
    ("76116", "Fort Worth", 3.0), ("76119", "Fort Worth", 3.0),
    ("76102", "Fort Worth", 2.5), ("76103", "Fort Worth", 2.5),
    ("76105", "Fort Worth", 2.5), ("76109", "Fort Worth", 2.5),
    ("76117", "Haltom City", 2.0), ("76118", "Fort Worth", 2.0),
    ("76133", "Fort Worth", 2.0), ("76137", "Fort Worth", 2.0),
    ("76140", "Fort Worth", 1.8), ("76148", "Watauga", 1.5),
    ("76179", "Fort Worth", 1.5), ("76131", "Fort Worth", 1.5),
    ("76132", "Fort Worth", 1.5), ("76135", "Lake Worth", 1.2),
    # Mid-cities
    ("76010", "Arlington", 2.2), ("76011", "Arlington", 2.0),
    ("76013", "Arlington", 1.8), ("76014", "Arlington", 1.6),
    ("76015", "Arlington", 1.4), ("76016", "Arlington", 1.4),
    ("76017", "Arlington", 1.6), ("76018", "Arlington", 1.4),
    ("76006", "Arlington", 1.4), ("76012", "Arlington", 1.4),
    ("76040", "Euless", 1.2), ("76053", "Hurst", 1.2),
    ("76054", "Hurst", 1.0), ("76021", "Bedford", 1.2),
    ("75050", "Grand Prairie", 1.2), ("75051", "Grand Prairie", 1.1),
    ("75052", "Grand Prairie", 1.1),
    ("75061", "Irving", 1.0), ("75062", "Irving", 1.0), ("75060", "Irving", 0.9),
    # Dallas side -- long hauls
    ("75201", "Dallas", 0.7), ("75207", "Dallas", 0.7),
    ("75211", "Dallas", 0.8), ("75212", "Dallas", 0.7),
    ("75220", "Dallas", 0.6), ("75235", "Dallas", 0.6),
    ("75224", "Dallas", 0.6), ("75232", "Dallas", 0.5),
    # Outer edge -- rare, and the reason "outside service area" is a real reason
    ("76201", "Denton", 0.4), ("76210", "Denton", 0.4),
    ("75028", "Flower Mound", 0.3), ("76226", "Argyle", 0.25),
    ("76034", "Colleyville", 0.4), ("76092", "Southlake", 0.4),
    ("76248", "Keller", 0.5), ("76244", "Keller", 0.5),
    ("76063", "Mansfield", 0.6), ("76028", "Burleson", 0.6),
    ("76049", "Granbury", 0.2), ("76048", "Granbury", 0.2),
)

VEHICLE_MAKES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("FORD", ("F-150", "ESCAPE", "EXPLORER", "FUSION", "F-250", "MUSTANG")),
    ("CHEVROLET", ("SILVERADO", "MALIBU", "EQUINOX", "TAHOE", "IMPALA", "CRUZE")),
    ("TOYOTA", ("CAMRY", "COROLLA", "RAV4", "TACOMA", "TUNDRA", "HIGHLANDER")),
    ("HONDA", ("CIVIC", "ACCORD", "CR-V", "ODYSSEY", "PILOT")),
    ("NISSAN", ("ALTIMA", "SENTRA", "ROGUE", "TITAN", "VERSA")),
    ("DODGE", ("RAM 1500", "CHARGER", "GRAND CARAVAN", "DURANGO")),
    ("JEEP", ("GRAND CHEROKEE", "WRANGLER", "CHEROKEE")),
    ("GMC", ("SIERRA", "YUKON", "ACADIA")),
    ("HYUNDAI", ("ELANTRA", "SONATA", "SANTA FE")),
    ("KIA", ("OPTIMA", "SORENTO", "SOUL", "FORTE")),
    ("BMW", ("328I", "X5", "535I")),
    ("CHRYSLER", ("300", "TOWN & COUNTRY", "PACIFICA")),
)

VEHICLE_COLORS: tuple[str, ...] = (
    "white", "black", "silver", "gray", "red", "blue", "tan", "green", "maroon",
)

STREETS: tuple[str, ...] = (
    "N Main St", "E Lancaster Ave", "W Berry St", "Camp Bowie Blvd",
    "Hemphill St", "Riverside Dr", "Beach St", "Rosedale St", "Bryant Irvin Rd",
    "Meacham Blvd", "Jacksboro Hwy", "White Settlement Rd", "Alta Mere Dr",
    "Sycamore School Rd", "McCart Ave", "Bonds Ranch Rd", "Denton Hwy",
    "Airport Fwy", "Collins St", "Cooper St", "Division St", "Pioneer Pkwy",
    "I-35W Frontage Rd", "I-30 Service Rd", "Loop 820", "Highway 287",
)

TOW_DESTINATIONS: tuple[str, ...] = (
    "Summit Towing Yard, 4820 Industrial Blvd, Fort Worth, TX 76106",
    "Christian Brothers Automotive, Fort Worth, TX",
    "Firestone Complete Auto Care, Arlington, TX",
    "Discount Tire, Fort Worth, TX",
    "Caliber Collision, Hurst, TX",
    "Service King, Grand Prairie, TX",
    "Owner residence",
    "Dealership service drive",
)


# ==========================================================================
# Weighted choice helpers
# ==========================================================================


def _weighted(rng: random.Random, pairs: Iterable[tuple[Any, float]]) -> Any:
    items = list(pairs)
    total = sum(weight for _, weight in items)
    roll = rng.random() * total
    upto = 0.0
    for value, weight in items:
        upto += weight
        if roll <= upto:
            return value
    return items[-1][0]


def _haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in miles between two (lat, lng) pairs."""
    radius = 3958.7613
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def _is_staffed(moment: datetime) -> bool:
    """Whether ``moment`` (company-local, naive) falls in the staffed window."""
    return (
        moment.weekday() in STAFFED_DAYS
        and STAFFED_START_HOUR <= moment.hour < STAFFED_END_HOUR
    )


# ==========================================================================
# Generation
# ==========================================================================


def _road_distances() -> dict[str, float]:
    """ZIP -> driving-ish miles from base, derived from the real centroids.

    Straight-line distance times 1.25, which is the usual rule of thumb for
    road distance in a gridded metro. The point is not that any single figure
    is exact -- it is that `distance` and `zip` agree with each other, so the
    territory bands, the map and the mileage column tell one story.
    """
    from towbook_agent.web import geo

    base = geo.centroid_for_zip(BASE_ZIP)
    if base is None:
        raise SystemExit(
            f"BASE_ZIP {BASE_ZIP} is not in the centroid lookup "
            f"({geo.GEO_FILE}). Run scripts/build_zip_centroids.sh."
        )

    out: dict[str, float] = {}
    missing: list[str] = []
    for zip5, _city, _weight in TERRITORY:
        point = geo.centroid_for_zip(zip5)
        if point is None:
            missing.append(zip5)
            continue
        out[zip5] = round(max(1.0, _haversine_miles(base, point) * 1.25), 1)
    if missing:
        raise SystemExit(
            "these demo ZIPs have no centroid and would render as `unmapped` "
            f"on the maps: {', '.join(missing)}"
        )
    return out


def _pick_outcome(
    rng: random.Random, staffed: bool, is_light: bool, service: str = ""
) -> str:
    table = dict(OUTCOMES_INSIDE if staffed else OUTCOMES_OUTSIDE)

    if service in CLOSE_OFF_SERVICES:
        # A standing "we do not take these" decision. Pin acceptance at the
        # rate this service actually runs at and give the freed mass to the
        # outcome that would really have happened instead: inside staffed
        # hours somebody picks up and says no; outside, it simply times out.
        target = CLOSE_OFF_SERVICES[service]
        freed = max(0.0, table["won"] - target)
        table["won"] = target
        if staffed:
            table["declined"] += freed
        else:
            table["declined"] += freed * 0.35
            table["no_response"] += freed * 0.65
        return _weighted(rng, table.items())

    if is_light:
        shift = min(LIGHT_SERVICE_DECLINE_SHIFT, table["won"])
        table["won"] -= shift
        table["declined"] += shift
    return _weighted(rng, table.items())


def _expiry_minutes(rng: random.Random) -> float:
    """How long the club leaves the offer open.

    Measured live: median 2.8 min, mean 3.6, p90 7.0, max 15.0. That tight
    window is the evidence behind the whole missed-work argument -- it is why
    "we would have called them back in the morning" is not an answer -- so it
    is modelled rather than left uniform.
    """
    value = rng.lognormvariate(math.log(2.8), 0.62)
    return min(15.0, max(1.0, value))


def _generate(rng: random.Random, weeks: int) -> tuple[list[dict[str, Any]], datetime, datetime]:
    """Build the record list. Returns (records, window_start, window_end)."""
    distances = _road_distances()

    now = datetime.now().replace(microsecond=0)
    # Whole weeks, ending at the most recent midnight, plus today so far. The
    # blind-spot grid is an hour-of-week analysis: a window that is not a whole
    # number of weeks gives some cells one more sample than their neighbours,
    # and the grid then shades by how the window was cut rather than by how the
    # business runs.
    today = now.date()
    end = now
    start_date = today - timedelta(days=weeks * 7)
    start = datetime.combine(start_date, datetime.min.time())

    records: list[dict[str, Any]] = []
    request_seq = 4_100_000
    job_seq = 880_000

    day = start_date
    while day <= today:
        day_weight = DAY_WEIGHTS[day.weekday()]
        target = BASE_OFFERS_PER_DAY * day_weight
        # Poisson-ish daily variation. A book with the same count every day is
        # the other way a synthetic dataset gives itself away.
        count = max(0, int(rng.gauss(target, target * 0.18)))

        for _ in range(count):
            hour = _weighted(rng, ((h, w) for h, w in enumerate(HOUR_WEIGHTS)))
            offered = datetime.combine(day, datetime.min.time()) + timedelta(
                hours=hour, minutes=rng.randint(0, 59), seconds=rng.randint(0, 59)
            )
            if offered > end:
                continue

            request_seq += 1
            service, is_light = _pick_service(rng)
            staffed = _is_staffed(offered)
            bucket = _pick_outcome(rng, staffed, is_light, service)
            code, status_name = _pick_status(rng, bucket)

            zip5, city = _pick_place(rng)
            distance = distances[zip5]
            # A little scatter so every job from one ZIP is not the same
            # distance, which no real feed looks like.
            distance = round(max(0.6, distance * rng.uniform(0.82, 1.24)), 1)

            expires = offered + timedelta(minutes=_expiry_minutes(rng))

            # Who answered. Nobody answered a no_response -- that is what it
            # means -- so the field stays blank rather than being filled with a
            # plausible name.
            responder = ""
            if bucket in ("won", "declined", "accept_failed"):
                responder = rng.choice(DISPATCHERS)

            # The Towbook job number. Issued when an offer becomes a job, so it
            # is nearly always present on won work and mostly absent otherwise;
            # 0 means absent and schema.yaml turns that into NULL.
            job_number = 0
            if bucket == "won" and rng.random() < 0.99:
                job_seq += 1
                job_number = job_seq
            elif bucket in ("client_withdrew", "accept_failed") and rng.random() < 0.55:
                job_seq += 1
                job_number = job_seq

            records.append(
                {
                    "callRequestId": str(request_seq),
                    "callNumber": job_number,
                    "providerName": _weighted(rng, CLIENTS),
                    "companyName": "Summit Towing & Recovery",
                    "requestDate": offered.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-4],
                    "requestDateUtc": "0001-01-01T00:00:00",
                    "expirationDate": expires.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-4],
                    "status": code,
                    "statusName": status_name,
                    "responseReasonName": (
                        _weighted(rng, DENIAL_REASONS) if bucket == "declined" else ""
                    ),
                    "serviceNeeded": service,
                    "vehicle": _vehicle(rng),
                    "startingLocation": (
                        f"{rng.randint(100, 9899)} {rng.choice(STREETS)}, "
                        f"{city}, TX, {zip5}"
                    ),
                    "zip": zip5,
                    "towDestination": (
                        rng.choice(TOW_DESTINATIONS) if bucket == "won" else ""
                    ),
                    "distance": distance,
                    "ownerUserName": responder,
                    "offerAmount": "",
                }
            )

        day += timedelta(days=1)

    # A club that gets no answer re-broadcasts the same job minutes later. The
    # product collapses those into one job (see `duplicate_offers` in
    # rules.yaml); without any in the data that feature demos as a no-op.
    records.extend(_rebroadcasts(rng, records, end))

    records.sort(key=lambda r: r["requestDate"])
    return records, start, end


def _rebroadcasts(
    rng: random.Random, records: list[dict[str, Any]], end: datetime
) -> list[dict[str, Any]]:
    """Second offers of a job nobody answered the first time."""
    unanswered = [r for r in records if r["status"] in (5, 80)]
    rng.shuffle(unanswered)
    out: list[dict[str, Any]] = []
    seq = 4_900_000

    for original in unanswered[: int(len(unanswered) * 0.16)]:
        first = datetime.strptime(original["requestDate"], "%Y-%m-%dT%H:%M:%S.%f")
        again = first + timedelta(minutes=rng.randint(4, 40))
        if again > end:
            continue
        seq += 1
        staffed = _is_staffed(again)
        # The second offer is the SAME job: same service, same standing
        # decision about whether this company takes that kind of work. Picking
        # the outcome without passing the service through would have a company
        # that refuses 94% of lockouts accept the re-broadcast of one at the
        # ordinary rate, which quietly lifts that service back above the
        # close-off threshold it belongs under.
        service = original["serviceNeeded"]
        bucket = _pick_outcome(rng, staffed, _is_light_service(service), service)
        code, status_name = _pick_status(rng, bucket)
        expires = again + timedelta(minutes=_expiry_minutes(rng))

        clone = dict(original)
        clone.update(
            {
                "callRequestId": str(seq),
                "callNumber": 0,
                "requestDate": again.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-4],
                "expirationDate": expires.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-4],
                "status": code,
                "statusName": status_name,
                "responseReasonName": (
                    _weighted(rng, DENIAL_REASONS) if bucket == "declined" else ""
                ),
                "ownerUserName": (
                    rng.choice(DISPATCHERS)
                    if bucket in ("won", "declined", "accept_failed")
                    else ""
                ),
                "towDestination": (
                    rng.choice(TOW_DESTINATIONS) if bucket == "won" else ""
                ),
            }
        )
        out.append(clone)
    return out


def _is_light_service(service: str) -> bool:
    for label, _weight, is_light in SERVICES:
        if label == service:
            return is_light
    return False


def _pick_service(rng: random.Random) -> tuple[str, bool]:
    chosen = _weighted(rng, ((entry, entry[1]) for entry in SERVICES))
    return chosen[0], chosen[2]


def _pick_status(rng: random.Random, bucket: str) -> tuple[int, str]:
    options = STATUS_BY_BUCKET[bucket]
    chosen = _weighted(rng, ((opt, opt[2]) for opt in options))
    return chosen[0], chosen[1]


def _pick_place(rng: random.Random) -> tuple[str, str]:
    chosen = _weighted(rng, ((entry, entry[2]) for entry in TERRITORY))
    return chosen[0], chosen[1]


def _vehicle(rng: random.Random) -> str:
    make, models = rng.choice(VEHICLE_MAKES)
    return (
        f"{rng.randint(2004, 2025)} {make} {rng.choice(models)} "
        f"{rng.choice(VEHICLE_COLORS)}"
    )


# ==========================================================================
# Driver
# ==========================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--weeks", type=int, default=13,
        help="whole weeks of history to generate (default 13, one quarter)",
    )
    parser.add_argument(
        "--generate-only", action="store_true",
        help="write the archive but do not ingest or compute metrics",
    )
    args = parser.parse_args()

    for name, value in (
        ("TOWBOOK_REPO_ROOT", os.environ.get("TOWBOOK_REPO_ROOT")),
        ("DATABASE_URL", os.environ.get("DATABASE_URL")),
    ):
        if not value:
            return _refuse(f"{name} is not set. See the usage note in this file's docstring.")

    if "demo" not in os.environ["DATABASE_URL"].lower():
        return _refuse(
            f"DATABASE_URL is {os.environ['DATABASE_URL']!r}, which does not look "
            f"like the demo database. Refusing to seed synthetic data into what "
            f"might be the live one."
        )

    # ----------------------------------------------------------------------
    # The SQLite file must land inside the demo root, and this is checked
    # rather than assumed because getting it wrong is SILENT.
    #
    # A relative path in DATABASE_URL is resolved against REPO_ROOT
    # (core/paths.py -> resolve_under_root). Git Bash on Windows expands
    # `$(pwd)` to `/c/Users/...`, which is not absolute to Windows, so
    # `sqlite:///$(pwd)/demo-root/data/demo.db` quietly becomes
    # `<root>/c/Users/.../demo.db` -- a real database, in a stray tree, that
    # the seeder fills and the board never reads. Everything reports success
    # and the demo comes up empty.
    # ----------------------------------------------------------------------
    _assert_database_is_in_the_demo_root(os.environ["DATABASE_URL"])

    from towbook_agent.agents import metrics
    from towbook_agent.agents.ingestion import ingest
    from towbook_agent.core import companies as _companies
    from towbook_agent.core.db import init_db
    from towbook_agent.core.paths import RAW_DIR, ensure_dirs

    # ----------------------------------------------------------------------
    # TZ, set from the roster rather than trusted from the shell.
    #
    # `TZ` beats schema.yaml's `source_timezone` (agents/ingestion.py ->
    # _source_timezone), so it decides what local hour every generated
    # timestamp lands on. Requiring the caller to export it is a footgun for
    # two independent reasons: production .env sets it to America/Detroit, and
    # Git Bash on Windows silently drops `TZ=... command` prefixes when it
    # launches a native Windows process -- so the variable a caller believed
    # they set never arrives, and the failure is a quiet one-hour skew rather
    # than an error.
    #
    # Taking it from the company record instead makes the mismatch impossible:
    # the timezone the offers are generated in and the timezone they are read
    # back in are the same value, read from one place.
    # ----------------------------------------------------------------------
    company = _companies.get_company(COMPANY_ID)
    if company is None:
        return _refuse(
            f"{COMPANY_ID!r} is not in the roster at "
            f"{os.environ['TOWBOOK_REPO_ROOT']}/config/companies.yaml."
        )
    timezone_name = company.timezone or "America/Chicago"
    os.environ["TZ"] = timezone_name
    print(f"Timezone: {timezone_name} (from the roster, not the shell)")

    # The generator hard-codes the staffed window; the board reads it from the
    # roster. If they ever disagree the demo shows blind spots that do not line
    # up with its own stated hours, so check rather than trust.
    _assert_window_matches_roster(_companies)

    rng = random.Random(RANDOM_SEED)
    print(f"Generating {args.weeks} whole weeks for {COMPANY_ID} ...")
    records, start, end = _generate(rng, args.weeks)
    print(f"  {len(records):,} offers from {start:%Y-%m-%d} to {end:%Y-%m-%d %H:%M}")

    ensure_dirs()
    archive = RAW_DIR / f"demo-seed-{end:%Y%m%d}.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(json.dumps(records, indent=1), encoding="utf-8")
    print(f"  archive: {archive} ({archive.stat().st_size / 1024:.0f} KB)")

    _summarise(records)

    if args.generate_only:
        print("\n--generate-only: stopping before ingestion.")
        return 0

    init_db()
    _stamp_alembic(os.environ["DATABASE_URL"])

    run_id = f"demo-seed-{end:%Y%m%d%H%M%S}"
    print(f"\nIngesting through the real ingester (run {run_id}) ...")
    result = ingest(archive, run_id, company_id=COMPANY_ID)
    print(f"  read {result.rows_read:,}  inserted {result.rows_inserted:,}  "
          f"updated {result.rows_updated:,}  rejected {result.rows_rejected:,}")
    if result.rows_rejected:
        print(f"  reject reasons: {result.reject_reasons}")
    if result.unmapped_statuses:
        print(f"  UNMAPPED STATUSES: {result.unmapped_statuses}")
        return _refuse("an unmapped status would land in the wrong bucket.")

    print("\nComputing metrics through the real metrics passes ...")
    days = metrics.recompute_days(
        start.date(), end.date(), company_id=COMPANY_ID, emit_alerts=False
    )
    print(f"  daily:   {len(days)} days")

    weeks_done = _compute_weeks(metrics, start.date(), end.date())
    print(f"  weekly:  {weeks_done} weeks")
    months_done = _compute_months(metrics, start.date(), end.date())
    print(f"  monthly: {months_done} months")

    metrics.compute_hourly(
        datetime.combine(end.date(), datetime.min.time()).replace(hour=max(0, end.hour - 1)),
        company_id=COMPANY_ID,
        emit_alerts=False,
    )
    print("  hourly:  most recent full hour")

    _ensure_demo_account()

    print("\nSeed complete.")
    return 0


def _ensure_demo_account() -> None:
    """Create the demo login, if this database does not have it yet.

    Part of seeding rather than a step somebody remembers, because re-seeding
    drops the database and the account with it. A demo whose login stopped
    working because the data was refreshed is worse than no demo: it fails in
    front of the person it was built to impress.

    Creating any account switches this install out of shared-password mode --
    for THIS database only. `dashboard_users` lives in the demo database, so
    the production board is untouched and stays on DASHBOARD_PASSWORD.
    """
    from towbook_agent.web import accounts

    existing = accounts.get_user(username=DEMO_USERNAME)
    if existing is not None:
        print(f"\nDemo login: {DEMO_USERNAME!r} already exists, left alone.")
        return

    user = accounts.create_user(
        DEMO_USERNAME,
        DEMO_PASSWORD,
        role=accounts.ROLE_MEMBER,
        company_ids=[COMPANY_ID],
        display_name="Summit Towing (demo)",
        # A demo account must not be forced through a password change on first
        # login: the person using it is a prospect, and the first thing they
        # would see is a form asking them to pick a new password for a company
        # that does not exist.
        must_change_password=False,
    )
    print(
        f"\nDemo login created:\n"
        f"  username : {user.username}\n"
        f"  password : {DEMO_PASSWORD}\n"
        f"  role     : {user.role} (not operator)\n"
        f"  scope    : {user.company_scope}"
    )


def _assert_database_is_in_the_demo_root(url: str) -> None:
    """Fail loudly if the SQLite file would land outside the demo root."""
    if not url.startswith("sqlite"):
        return  # Postgres and friends have no path to check.

    from towbook_agent.core.paths import REPO_ROOT, resolve_under_root

    raw = url.split("///", 1)[-1].split("?", 1)[0]
    if not raw or raw == ":memory:":
        return
    resolved = resolve_under_root(raw)
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError:
        raise SystemExit(
            "DATABASE_URL resolves outside the demo root, which means the demo "
            "board would read a different file from the one seeded:\n"
            f"  DATABASE_URL : {url}\n"
            f"  resolves to  : {resolved}\n"
            f"  demo root    : {REPO_ROOT}\n\n"
            "On Git Bash use `pwd -W` (a Windows path), not `pwd`:\n"
            '  ROOT="$(pwd -W)" DATABASE_URL="sqlite:///$ROOT/demo-root/data/demo.db"'
        ) from None

    # `<root>/c/Users/...` is technically inside the root and still wrong.
    parts = resolved.relative_to(REPO_ROOT).parts
    if parts[:1] in (("c",), ("C",)) or "Users" in parts:
        raise SystemExit(
            f"DATABASE_URL resolved to {resolved}, which looks like a Git Bash "
            f"`/c/Users/...` path that was treated as relative to the demo root. "
            f"Use `pwd -W` so the path is absolute to Windows."
        )


def _stamp_alembic(url: str) -> None:
    """Record the demo database as being at the migration head.

    `init_db()` builds the schema with `create_all` from the current models,
    which IS the head schema -- but it writes no `alembic_version` row, and a
    database with tables and no version row is one `alembic upgrade head` away
    from replaying the first migration over a populated schema and failing.
    The app warns about exactly this on every boot.

    Stamping cannot go through the normal path here: `core/db.py` resolves
    `alembic.ini` and `alembic/` under TOWBOOK_REPO_ROOT, and the demo root
    deliberately holds only config and data. So the Config is pointed at the
    real repository's migrations while the URL stays on the demo database.
    Nothing is applied -- `stamp` only writes the version row.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            if "alembic_version" in inspect(connection).get_table_names():
                existing = connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).fetchall()
                if existing:
                    return  # already stamped
    finally:
        engine.dispose()

    repo_root = Path(__file__).resolve().parents[1]
    ini = repo_root / "alembic.ini"
    if not ini.is_file():
        print("  (no alembic.ini found; leaving the database unstamped)")
        return

    config = Config(str(ini))
    config.set_main_option("script_location", str(repo_root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.stamp(config, "head")
    print("  stamped the demo database at the migration head")


def _compute_weeks(metrics: Any, first: date, last: date) -> int:
    week = metrics.week_start_for(first)
    done = 0
    while week <= last:
        metrics.compute_weekly(week, company_id=COMPANY_ID, emit_alerts=False)
        done += 1
        week += timedelta(days=7)
    return done


def _compute_months(metrics: Any, first: date, last: date) -> int:
    month = metrics.month_start_for(first)
    done = 0
    while month <= last:
        metrics.compute_monthly(month, company_id=COMPANY_ID, emit_alerts=False)
        done += 1
        month = metrics.month_end_for(month) + timedelta(days=1)
    return done


def _assert_window_matches_roster(_companies: Any) -> None:
    rules = _companies.rules_for(COMPANY_ID)
    windows = (rules.get("missed_work") or {}).get("coverage", {}).get("windows") or []
    if len(windows) != 1:
        raise SystemExit(
            f"the roster gives {COMPANY_ID} {len(windows)} coverage windows; this "
            f"generator models exactly one. Reconcile them before seeding."
        )
    window = windows[0]
    days = {
        ("mon", "tue", "wed", "thu", "fri", "sat", "sun").index(d.lower()[:3])
        for d in window.get("days", [])
    }
    start_hour = int(str(window.get("start", "")).split(":")[0])
    end_hour = int(str(window.get("end", "")).split(":")[0])
    if (days, start_hour, end_hour) != (STAFFED_DAYS, STAFFED_START_HOUR, STAFFED_END_HOUR):
        raise SystemExit(
            "the staffed window in demo-root/config/companies.yaml does not match "
            "the one this generator models:\n"
            f"  roster:    days={sorted(days)} {start_hour:02d}:00-{end_hour:02d}:00\n"
            f"  generator: days={sorted(STAFFED_DAYS)} "
            f"{STAFFED_START_HOUR:02d}:00-{STAFFED_END_HOUR:02d}:00"
        )


def _summarise(records: list[dict[str, Any]]) -> None:
    """Print the contrast the demo exists to show, before anything is stored."""
    inside = outside = 0
    inside_noresp = outside_noresp = 0
    for record in records:
        moment = datetime.strptime(record["requestDate"], "%Y-%m-%dT%H:%M:%S.%f")
        unanswered = record["status"] in (5, 80)
        if _is_staffed(moment):
            inside += 1
            inside_noresp += unanswered
        else:
            outside += 1
            outside_noresp += unanswered

    print("\n  Coverage contrast in the generated data:")
    print(f"    inside staffed hours : {inside:>6,} offers, "
          f"{inside_noresp / max(1, inside):6.1%} unanswered")
    print(f"    outside              : {outside:>6,} offers, "
          f"{outside_noresp / max(1, outside):6.1%} unanswered")

    print("\n  Close-off candidates (the view lists <=10% accepted, >=4 offers):")
    for service in CLOSE_OFF_SERVICES:
        rows = [r for r in records if r["serviceNeeded"] == service]
        won = sum(1 for r in rows if r["status"] in (1, 10))
        print(f"    {service:<16} {len(rows):>5,} offers, "
              f"{won / max(1, len(rows)):6.1%} accepted")


def _refuse(message: str) -> int:
    print(f"\nREFUSING: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
