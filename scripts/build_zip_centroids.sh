#!/usr/bin/env bash
# ==========================================================================
# build_zip_centroids.sh -- regenerate towbook_agent/web/geo/oh_zip_centroids.json
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
# `ZIP,LAT,LNG` CSV. Only Ohio ZIPs (43xxx/44xxx/45xxx) are kept -- the service
# market is central Ohio and the "outside" territory band reaches no further
# than the rest of the state. A job whose ZIP is not in the file is reported as
# `unmapped` on the page rather than silently dropped.
#
# Usage:
#   scripts/build_zip_centroids.sh
# ==========================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$HERE/towbook_agent/web/geo/oh_zip_centroids.json"
SRC_URL="https://gist.githubusercontent.com/erichurst/7882666/raw/5bdc46db47d9515269ab12ed6fb2850377fd869e/US%20Zip%20Codes%20from%202013%20Government%20Data"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

echo "downloading ZIP centroids ..."
curl -fsSL "$SRC_URL" -o "$tmp"

count="$(grep -cE '^4[345][0-9]{3},' "$tmp")"
echo "Ohio ZIPs found: $count"

mkdir -p "$(dirname "$OUT")"
{
  printf '{\n'
  printf '  "source": "US ZIP code centroids from 2013 US Government data (public domain), via gist erichurst/7882666. One-time build artifact; regenerate with scripts/build_zip_centroids.sh.",\n'
  printf '  "state": "OH",\n'
  printf '  "format": "zip -> [latitude, longitude]",\n'
  printf '  "count": %s,\n' "$count"
  printf '  "zips": {'
  awk -F',' 'NR>1 && $1 ~ /^4[345][0-9][0-9][0-9]$/ {
      z=$1; la=$2; lo=$3; gsub(/[" ]/,"",z); gsub(/[ ]/,"",la); gsub(/[ ]/,"",lo);
      printf "%s\n    \"%s\": [%s, %s]", sep, z, la, lo; sep="," }' "$tmp"
  printf '\n  }\n}\n'
} > "$OUT"

echo "wrote $OUT ($(wc -c < "$OUT") bytes)"
