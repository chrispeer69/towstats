"""ZIP-centroid geocoding for the maps views.

WHY A LOOKUP AND NOT A GEOCODER
-------------------------------
The Towbook feed carries no latitude/longitude. It carries a ZIP -- the API
states it as its own field, populated on 3,122 of 3,124 archived records (see
``config/schema.yaml`` and ``requests.pickup_zip``) -- so a job is placed on the
map at the centroid of its pickup ZIP. The centroids live in a committed data
file (``geo/oh_zip_centroids.json``), exactly like the vendored Leaflet and
Chart.js: the running app needs no network, no API key and no geocoding service,
and the same input always yields the same point. A ZIP that is not in the file
is reported as ``unmapped`` on the page rather than silently dropped -- the same
"a missing number is visible, never invented" rule the rest of the dashboard
follows.

ZIP-centroid precision is city/neighbourhood level, which is exactly the grain
the two questions need: *where is work concentrated, and where is it not*, and
*which declined jobs sit where*. It is deliberately NOT street-level: the feed's
free-text address is shown in the popup for the human, but the marker is placed
on the ZIP so two addresses written six different ways still land together.

Multiple jobs in the same ZIP would otherwise stack on one pixel, so
:func:`place` spreads them deterministically around the centroid with a small
sunflower offset -- same job, same spot, every render, and no two markers hidden
behind each other.
"""

from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "centroid_for_zip",
    "normalize_zip",
    "zip_from_text",
    "place",
    "centroid_count",
    "GEO_FILE",
]

#: The committed lookup. Lives beside this module so it ships with the package
#: on disk (Railway runs from source, so there is nothing to package-data), and
#: is never served over HTTP -- the web layer reads it, the browser never does.
GEO_FILE: Path = Path(__file__).resolve().parent / "geo" / "oh_zip_centroids.json"

#: A US ZIP is five digits; ZIP+4 arrives as "43201-1234". The API field is
#: usually already the bare five, but the address-text fallback below has to
#: cope with either. A leading-zero ZIP would be mangled if it were ever read as
#: a number, so everything here is strings, start to finish.
_ZIP5 = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


@lru_cache(maxsize=1)
def _centroids() -> dict[str, tuple[float, float]]:
    """The ``{zip: (lat, lng)}`` lookup, read once and cached.

    A missing or corrupt file degrades to an empty lookup: the maps then show
    every job as ``unmapped`` and say so, which is a truthful empty state, not a
    crash. The maps are one view; they must never be able to take the board down.
    """
    try:
        raw = json.loads(GEO_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    zips = raw.get("zips") if isinstance(raw, dict) else None
    if not isinstance(zips, dict):
        return {}
    out: dict[str, tuple[float, float]] = {}
    for key, value in zips.items():
        try:
            lat, lng = float(value[0]), float(value[1])
        except (TypeError, ValueError, IndexError):
            continue
        out[str(key).strip()] = (lat, lng)
    return out


def centroid_count() -> int:
    """How many ZIP centroids are loaded. Surfaced on the page for transparency."""
    return len(_centroids())


def normalize_zip(value: Any) -> str | None:
    """A bare five-digit ZIP from ``value``, or ``None``.

    Accepts an int (leading zeros already lost, left-padded back), a "43201",
    a "43201-1234", or "43201.0" as some exports render a numeric cell. Anything
    with no five-digit core returns ``None``.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        text = f"{int(value):05d}"
    else:
        text = str(value).strip()
    if not text:
        return None
    match = _ZIP5.search(text)
    return match.group(1) if match else None


def zip_from_text(text: Any) -> str | None:
    """Best-effort ZIP pulled out of a free-text address.

    The FALLBACK only, for a CSV-ingested row that never carried a ``zip`` field
    of its own -- the API path fills ``pickup_zip`` directly and never reaches
    here. Territory decisions in ``rules.yaml`` deliberately refuse to regex the
    address because a boundary that moves with formatting is not a boundary; a
    map pin is far lower stakes, so a best-effort ZIP here is a reasonable way to
    place a point that would otherwise be unmapped. Returns ``None`` when the
    address carries no five-digit run.
    """
    if not text:
        return None
    match = _ZIP5.search(str(text))
    return match.group(1) if match else None


def centroid_for_zip(value: Any) -> tuple[float, float] | None:
    """``(lat, lng)`` for a ZIP, or ``None`` if it is unknown/out of state."""
    zip5 = normalize_zip(value)
    if zip5 is None:
        return None
    return _centroids().get(zip5)


def place(lat: float, lng: float, index: int, total: int) -> tuple[float, float]:
    """Spread ``total`` markers that share one centroid so none hides another.

    A deterministic sunflower (phyllotaxis) offset: point ``index`` of ``total``
    is pushed out along the golden angle by a radius that grows with the count,
    so ten jobs in one ZIP become a tidy little cluster instead of a single
    pixel with nine markers stacked invisibly behind it. Deterministic in
    ``(index, total)``, so a job lands in the same place on every render.

    A single job (``total <= 1``) sits exactly on the centroid.
    """
    if total <= 1:
        return (lat, lng)
    # Golden angle in radians. ~0.30 miles max spread at the outside of a busy
    # ZIP: enough to separate markers, small enough to stay inside the ZIP.
    golden = math.pi * (3.0 - math.sqrt(5.0))
    # Normalised radius 0..1 across the cluster, then scaled to degrees. One
    # degree of latitude is ~69 miles; ~0.004 deg is ~0.28 mi.
    frac = math.sqrt((index + 0.5) / total)
    radius_deg = 0.004 * frac
    angle = index * golden
    # Longitude degrees shrink with latitude; correct so the cluster stays round.
    cos_lat = max(math.cos(math.radians(lat)), 0.2)
    d_lat = radius_deg * math.sin(angle)
    d_lng = (radius_deg * math.cos(angle)) / cos_lat
    return (lat + d_lat, lng + d_lng)
