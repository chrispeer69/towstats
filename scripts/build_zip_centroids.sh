#!/usr/bin/env bash
# ==========================================================================
# build_zip_centroids.sh -- regenerate towbook_agent/web/geo/us_zip_centroids.json
#
# The maps views place each job at the centroid of its pickup ZIP. There are no
# latitude/longitude fields in the Towbook feed (verified -- see schema.yaml),
# so the ZIP the API already supplies (`pickup_zip`, populated on 3,122 of 3,124
# records) is geocoded against this committed lookup. It is a ONE-TIME BUILD
# ARTIFACT, checked in like the vendored Leaflet/Chart.js, so the running app
# needs no network and no geocoding service and is fully deterministic.
#
# Source: US ZIP code centroids derived from 2013 US Government data (public
# domain), mirrored at gist.github.com/erichurst/7882666 as a plain
# `ZIP,LAT,LNG` CSV.
#
# NATIONAL COVERAGE. This file used to keep only Ohio ZIPs (43xxx/44xxx/45xxx),
# because every tenant was in central Ohio. That made the maps silently empty
# for a tenant anywhere else -- a job whose ZIP is not in the file is reported
# as `unmapped` rather than dropped, which is honest but is still a blank map.
# Keeping all 33,144 US ZIPs costs about 1.1 MB and removes that as a thing to
# remember when onboarding a customer outside Ohio.
#
# Coordinates are rounded to 4 decimal places (~11 m). A ZIP centroid is a
# city/neighbourhood-grain approximation to begin with, so the rounding is
# three orders of magnitude below the error already inherent in the method,
# and it keeps the committed file about 25% smaller.
#
# Usage:
#   scripts/build_zip_centroids.sh
# ==========================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$HERE/towbook_agent/web/geo/us_zip_centroids.json"
SRC_URL="https://gist.githubusercontent.com/erichurst/7882666/raw/5bdc46db47d9515269ab12ed6fb2850377fd869e/US%20Zip%20Codes%20from%202013%20Government%20Data"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

echo "downloading ZIP centroids ..."
curl -fsSL "$SRC_URL" -o "$tmp"

count="$(awk -F',' 'NR>1 && $1 ~ /^[0-9][0-9][0-9][0-9][0-9]$/' "$tmp" | wc -l | tr -d ' ')"
echo "US ZIPs found: $count"

mkdir -p "$(dirname "$OUT")"
{
  printf '{\n'
  printf '  "source": "US ZIP code centroids from 2013 US Government data (public domain), via gist erichurst/7882666. One-time build artifact; regenerate with scripts/build_zip_centroids.sh.",\n'
  printf '  "state": "US",\n'
  printf '  "format": "zip -> [latitude, longitude], rounded to 4dp (~11 m, far finer than ZIP-centroid grain)",\n'
  printf '  "count": %s,\n' "$count"
  printf '  "zips": {'
  awk -F',' 'NR>1 && $1 ~ /^[0-9][0-9][0-9][0-9][0-9]$/ {
      z=$1; la=$2+0; lo=$3+0;
      gsub(/[" ]/,"",z);
      if (la == 0 && lo == 0) next;
      printf "%s\n    \"%s\": [%.4f, %.4f]", sep, z, la, lo; sep="," }' "$tmp"
  printf '\n  }\n}\n'
} > "$OUT"

echo "wrote $OUT ($(wc -c < "$OUT") bytes)"
