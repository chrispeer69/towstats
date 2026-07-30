/* ==========================================================================
 * maps.js -- the two job maps. Application code, not a vendored library.
 * ==========================================================================
 *
 * Reads the window's data from #maps-data (embedded by maps.html, so the maps
 * need no second round-trip) and draws two Leaflet maps:
 *
 *   #offered-map   a heat layer of every offer, weighted per pickup ZIP.
 *   #declines-map  one circle marker per not-accepted job, popup with the
 *                  Towbook reference, service, offer time, who actioned it, and
 *                  the decline reason.
 *
 * Colours come from the same CSS custom properties the charts read, so the maps
 * belong to whichever theme the board is in. Popup text is set with textContent,
 * never innerHTML: it carries client-entered strings (addresses, reasons) and
 * must never be parsed as markup.
 *
 * The one external dependency is the basemap tiles (OpenStreetMap by default,
 * configurable in rules.yaml). If the tile URL is blank or the tiles fail to
 * load, the heat and the markers still render on a plain background -- the data
 * is ours and is drawn regardless.
 * ========================================================================== */

(function (global) {
  "use strict";

  function cssVar(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name);
    value = (value || "").trim();
    return value || fallback;
  }

  function bucketColor(bucket) {
    switch (bucket) {
      case "no_response":
        return cssVar("--map-no-response", "#d03b3b");
      case "declined":
        return cssVar("--map-declined", "#e08a1e");
      case "client_withdrew":
        return cssVar("--map-withdrew", "#7c8792");
      case "accept_failed":
        return cssVar("--map-accept-failed", "#9085e9");
      default:
        return cssVar("--muted", "#888888");
    }
  }

  function heatGradient() {
    /* Cold -> hot, built from the board's own palette so density reads at a
       glance on a light basemap: blue where work is thin, red where it is
       dense. Keys are the 0..1 stops leaflet.heat expects. */
    return {
      0.0: cssVar("--seq-1", "#9ec5f4"),
      0.4: cssVar("--seq-3", "#3987e5"),
      0.65: cssVar("--warning", "#fab219"),
      0.85: cssVar("--serious", "#ec835a"),
      1.0: cssVar("--critical", "#d03b3b")
    };
  }

  function readData() {
    var node = document.getElementById("maps-data");
    if (!node) return null;
    try {
      return JSON.parse(node.getAttribute("data-maps") || "null");
    } catch (err) {
      if (global.console) global.console.error("maps: bad payload", err);
      return null;
    }
  }

  function addTiles(map, tiles) {
    if (!tiles || !tiles.url) return;
    try {
      global.L.tileLayer(tiles.url, {
        attribution: tiles.attribution || "",
        maxZoom: 19,
        subdomains: "abc"
      }).addTo(map);
    } catch (err) {
      /* A bad tile URL must not stop the data layers drawing. */
      if (global.console) global.console.warn("maps: tiles failed", err);
    }
  }

  function fit(map, latlngs, center, zoom) {
    if (latlngs && latlngs.length) {
      try {
        map.fitBounds(global.L.latLngBounds(latlngs).pad(0.15));
        return;
      } catch (err) {
        /* fall through to the default view */
      }
    }
    map.setView(center, zoom);
  }

  /* --------------------------------------------------------- offered heat */

  function buildOffered(data) {
    var el = document.getElementById("offered-map");
    if (!el || !global.L) return;
    var map = global.L.map(el, { scrollWheelZoom: false, attributionControl: true });
    addTiles(map, data.tiles);

    var points = (data.offered && data.offered.points) || [];
    var max = (data.offered && data.offered.max_weight) || 1;
    var latlngs = points.map(function (p) {
      return [p[0], p[1]];
    });

    if (points.length && global.L.heatLayer) {
      global.L.heatLayer(points, {
        radius: 28,
        blur: 20,
        minOpacity: 0.35,
        max: max,
        gradient: heatGradient()
      }).addTo(map);
    }
    fit(map, latlngs, data.center, data.zoom);
    setTimeout(function () {
      map.invalidateSize();
    }, 60);
  }

  /* ------------------------------------------------------- declined markers */

  function field(parent, label, value) {
    if (value === null || value === undefined || value === "") return;
    var row = document.createElement("div");
    row.className = "pop-row";
    var k = document.createElement("span");
    k.className = "pop-k";
    k.textContent = label;
    var v = document.createElement("span");
    v.className = "pop-v";
    v.textContent = String(value); /* textContent: never parse DB text as HTML */
    row.appendChild(k);
    row.appendChild(v);
    parent.appendChild(row);
  }

  function popup(marker) {
    var box = document.createElement("div");
    box.className = "map-popup";

    var head = document.createElement("div");
    head.className = "pop-head";
    head.textContent = marker.outcome_label + (marker.is_light ? " · light service" : "");
    box.appendChild(head);

    field(box, "Towbook ref", marker.ref_label || marker.ref || "—");
    field(box, "Service", marker.service_type);
    field(box, "Offered", marker.offered_label);
    field(box, "Responded", marker.responded ? "Yes" : "No");
    field(box, "By", marker.responded_by || "—");
    field(box, "Reason", marker.reason || "—");
    field(box, "Pickup", marker.address);
    return box;
  }

  function buildDeclines(data) {
    var el = document.getElementById("declines-map");
    if (!el || !global.L) return;
    var map = global.L.map(el, { scrollWheelZoom: false, attributionControl: true });
    addTiles(map, data.tiles);

    var markers = (data.declines && data.declines.markers) || [];
    var latlngs = [];
    var lightStroke = cssVar("--map-light-ring", "#f2c94c");

    markers.forEach(function (mk) {
      var color = bucketColor(mk.outcome);
      var circle = global.L.circleMarker([mk.lat, mk.lng], {
        radius: mk.is_light ? 7 : 6,
        color: mk.is_light ? lightStroke : color,
        weight: mk.is_light ? 2.5 : 1,
        fillColor: color,
        fillOpacity: 0.85
      });
      circle.bindPopup(popup(mk), { closeButton: true, maxWidth: 320 });
      circle.bindTooltip(
        mk.outcome_label + " · " + mk.service_type,
        { direction: "top", opacity: 0.9 }
      );
      circle.addTo(map);
      latlngs.push([mk.lat, mk.lng]);
    });

    fit(map, latlngs, data.center, data.zoom);
    setTimeout(function () {
      map.invalidateSize();
    }, 60);
  }

  function init() {
    if (!global.L) {
      if (global.console) global.console.error("maps: Leaflet not loaded");
      return;
    }
    var data = readData();
    if (!data) return;
    buildOffered(data);
    buildDeclines(data);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})(window);
