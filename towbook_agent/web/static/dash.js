/* ==========================================================================
 * dash.js -- dashboard behaviour. Application code, not a vendored library.
 * ==========================================================================
 *
 * Three jobs:
 *
 *  1. Theme. Dark by default, light available, the choice remembered. Chart
 *     colours are read from CSS custom properties at build time, so switching
 *     theme rebuilds every chart against the palette steps chosen for that
 *     surface -- a dark chart is not a flipped light chart.
 *
 *  2. Charts. A canvas declares what it is and carries its own data:
 *
 *        <canvas data-chart="live-hours" data-chart-data='{...}'></canvas>
 *
 *     dash.js scans for those on load AND after every HTMX swap. That indirect
 *     route is deliberate: HTML injected via innerHTML never executes inline
 *     <script>, so a chart defined by a script tag inside a swapped fragment
 *     would silently vanish the first time the date picker was used.
 *
 *  3. Small interactions: heatmap cell tooltips, and confirm-before-submit on
 *     the rules accept/reject forms.
 *
 * Every chart config below is a valid Chart.js v4 config. Replacing the
 * vendored shim in static/chart.umd.min.js with the real library changes
 * nothing here.
 * ========================================================================== */

(function (global) {
  "use strict";

  var TBK = {};
  var THEME_KEY = "tbk-theme";
  var built = [];

  /* ----------------------------------------------------------------- theme */

  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") {
      document.documentElement.setAttribute("data-theme", theme);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }

  function currentTheme() {
    var stored = null;
    try {
      stored = global.localStorage.getItem(THEME_KEY);
    } catch (err) {
      stored = null;
    }
    if (stored) return stored;
    return global.matchMedia && global.matchMedia("(prefers-color-scheme: light)").matches
      ? "light"
      : "dark";
  }

  TBK.toggleTheme = function () {
    var next = currentTheme() === "dark" ? "light" : "dark";
    try {
      global.localStorage.setItem(THEME_KEY, next);
    } catch (err) {
      /* private browsing -- theme just won't persist */
    }
    applyTheme(next);
    updateToggleLabel();
    TBK.rebuildCharts();
  };

  function updateToggleLabel() {
    var button = document.getElementById("theme-toggle");
    if (!button) return;
    var dark = currentTheme() === "dark";
    button.textContent = dark ? "☀ Light" : "☾ Dark";
    button.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
  }

  /* --------------------------------------------------------------- palette */

  function cssVar(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name);
    value = (value || "").trim();
    return value || fallback || "#888888";
  }
  TBK.cssVar = cssVar;

  TBK.palette = function () {
    return {
      series: [
        cssVar("--series-1", "#3987e5"),
        cssVar("--series-2", "#d95926"),
        cssVar("--series-3", "#199e70"),
        cssVar("--series-4", "#c98500"),
        cssVar("--series-5", "#d55181"),
        cssVar("--series-6", "#008300"),
        cssVar("--series-7", "#9085e9"),
        cssVar("--series-8", "#e66767")
      ],
      good: cssVar("--good", "#0ca30c"),
      warning: cssVar("--warning", "#fab219"),
      critical: cssVar("--critical", "#d03b3b"),
      grid: cssVar("--grid", "#2c2c2a"),
      muted: cssVar("--muted", "#898781"),
      text2: cssVar("--text-2", "#c3c2b7"),
      surface: cssVar("--surface", "#1a1a19")
    };
  };

  /* -------------------------------------------------------------- format */

  function pct(value, decimals) {
    if (value === null || value === undefined) return "—";
    return (value * 100).toFixed(decimals === undefined ? 0 : decimals) + "%";
  }
  TBK.pct = pct;

  function countAxis(grid) {
    return {
      beginAtZero: true,
      grid: { color: grid },
      ticks: { color: cssVar("--muted") }
    };
  }

  function percentAxis(grid) {
    return {
      min: 0,
      max: 1,
      stepSize: 0.25,
      beginAtZero: true,
      grid: { color: grid },
      ticks: {
        color: cssVar("--muted"),
        callback: function (value) {
          return Math.round(value * 100) + "%";
        }
      }
    };
  }

  function baseOptions(scaleY, grid) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      scales: {
        y: scaleY,
        x: { grid: { display: false }, ticks: { color: cssVar("--muted") } }
      },
      plugins: {
        legend: { display: false },
        tooltip: { enabled: true, callbacks: {} }
      }
    };
  }

  /* ------------------------------------------------------------- builders */

  var builders = {};

  /* Live: offered vs accepted per hour of today. Counts only -- the rate lives
     in its own chart below it, because two units never share one axis. */
  builders["live-hours"] = function (payload) {
    var colors = TBK.palette();
    var options = baseOptions(countAxis(colors.grid), colors.grid);
    options.plugins.tooltip.callbacks = {
      title: function (items) {
        return items[0].label + " – " + items[0].label.slice(0, 2) + ":59";
      },
      label: function (item) {
        var row = payload.hours[item.dataIndex];
        if (item.datasetIndex === 0) return "Offered: " + row.offered;
        return "Accepted: " + row.accepted + "  (" + pct(row.rate) + ")";
      }
    };
    return {
      type: "bar",
      data: {
        labels: payload.hours.map(function (h) {
          return h.label.slice(0, 2);
        }),
        datasets: [
          {
            label: "Offered",
            data: payload.hours.map(function (h) {
              return h.offered;
            }),
            backgroundColor: colors.series[0]
          },
          {
            label: "Accepted",
            data: payload.hours.map(function (h) {
              return h.accepted;
            }),
            backgroundColor: colors.good
          }
        ]
      },
      options: options
    };
  };

  /* Live: today's running acceptance rate against the 7d and 30d baselines. */
  builders["live-rate"] = function (payload) {
    var colors = TBK.palette();
    var options = baseOptions(percentAxis(colors.grid), colors.grid);
    options.plugins.tooltip.callbacks = {
      title: function (items) {
        return "Through " + items[0].label + ":59";
      },
      label: function (item) {
        if (item.datasetIndex === 0) return "Today running: " + pct(item.raw, 1);
        if (item.datasetIndex === 1) return "7-day average: " + pct(item.raw, 1);
        return "30-day average: " + pct(item.raw, 1);
      }
    };
    var labels = payload.hours.map(function (h) {
      return h.label.slice(0, 2);
    });
    var flat = function (value) {
      return labels.map(function () {
        return value === null || value === undefined ? null : value;
      });
    };
    return {
      type: "line",
      data: {
        labels: labels,
        datasets: [
          {
            label: "Today (running)",
            data: payload.hours.map(function (h) {
              return h.running_rate;
            }),
            borderColor: colors.series[0],
            borderWidth: 2.5,
            pointRadius: 0
          },
          {
            label: "7-day average",
            data: flat(payload.baseline_7d ? payload.baseline_7d.rate : null),
            borderColor: colors.series[1],
            borderWidth: 2,
            borderDash: [6, 4],
            pointRadius: 0
          },
          {
            label: "30-day average",
            data: flat(payload.baseline_30d ? payload.baseline_30d.rate : null),
            borderColor: colors.muted,
            borderWidth: 2,
            borderDash: [2, 4],
            pointRadius: 0
          }
        ]
      },
      options: options
    };
  };

  /* Daily: the same hour profile for a finished day. */
  builders["daily-hours"] = function (payload) {
    var colors = TBK.palette();
    var options = baseOptions(countAxis(colors.grid), colors.grid);
    options.plugins.tooltip.callbacks = {
      title: function (items) {
        return items[0].label + ":00";
      },
      label: function (item) {
        var row = payload.hours[item.dataIndex];
        if (item.datasetIndex === 0) return "Offered: " + row.offered;
        return "Accepted: " + row.accepted + "  (" + pct(row.rate) + ")";
      }
    };
    return {
      type: "bar",
      data: {
        labels: payload.hours.map(function (h) {
          return h.label.slice(0, 2);
        }),
        datasets: [
          {
            label: "Offered",
            data: payload.hours.map(function (h) {
              return h.offered;
            }),
            backgroundColor: colors.series[0]
          },
          {
            label: "Accepted",
            data: payload.hours.map(function (h) {
              return h.accepted;
            }),
            backgroundColor: colors.good
          }
        ]
      },
      options: options
    };
  };

  /* Trends: offer volume over time. */
  builders["trend-volume"] = function (payload) {
    var colors = TBK.palette();
    var options = baseOptions(countAxis(colors.grid), colors.grid);
    options.plugins.tooltip.callbacks = {
      title: function (items) {
        return payload.volume[items[0].dataIndex].label;
      },
      label: function (item) {
        var row = payload.volume[item.dataIndex];
        if (item.datasetIndex === 0) return "Offered: " + row.offered;
        return "Accepted: " + row.accepted + "  (" + pct(row.rate) + ")";
      }
    };
    return {
      type: "bar",
      data: {
        labels: payload.volume.map(function (v) {
          return v.label;
        }),
        datasets: [
          {
            label: "Offered",
            data: payload.volume.map(function (v) {
              return v.offered;
            }),
            backgroundColor: colors.series[0]
          },
          {
            label: "Accepted",
            data: payload.volume.map(function (v) {
              return v.accepted;
            }),
            backgroundColor: colors.good
          }
        ]
      },
      options: options
    };
  };

  /* Trends: weekly acceptance-rate trajectory per client, plus the overall
     line drawn in chrome grey so it reads as a reference, not a competitor. */
  builders["trend-trajectories"] = function (payload) {
    var colors = TBK.palette();
    var options = baseOptions(percentAxis(colors.grid), colors.grid);
    options.plugins.tooltip.callbacks = {
      title: function (items) {
        return "Week of " + items[0].label;
      },
      label: function (item) {
        var name = item.dataset.label;
        if (item.raw === null || item.raw === undefined) return null;
        return name + ": " + pct(item.raw);
      }
    };
    var datasets = payload.trajectories.map(function (entry, index) {
      return {
        label: entry.client_name,
        data: entry.points.map(function (p) {
          return p.rate;
        }),
        borderColor: colors.series[index % colors.series.length],
        borderWidth: 2,
        pointRadius: 3
      };
    });
    datasets.unshift({
      label: "All clients",
      data: payload.overall_weekly.map(function (w) {
        return w.rate;
      }),
      borderColor: colors.muted,
      borderWidth: 2.5,
      borderDash: [5, 4],
      pointRadius: 0
    });
    return {
      type: "line",
      data: {
        labels: payload.overall_weekly.map(function (w) {
          return w.label;
        }),
        datasets: datasets
      },
      options: options
    };
  };

  /* Client detail: daily volume, and the rate trajectory below it. */
  builders["client-volume"] = function (payload) {
    var colors = TBK.palette();
    var options = baseOptions(countAxis(colors.grid), colors.grid);
    options.plugins.tooltip.callbacks = {
      title: function (items) {
        return payload.trajectory[items[0].dataIndex].date;
      },
      label: function (item) {
        var row = payload.trajectory[item.dataIndex];
        if (item.datasetIndex === 0) return "Offered: " + row.offered;
        return "Accepted: " + row.accepted + "  (" + pct(row.rate) + ")";
      }
    };
    return {
      type: "bar",
      data: {
        labels: payload.trajectory.map(function (t) {
          return t.date.slice(5);
        }),
        datasets: [
          {
            label: "Offered",
            data: payload.trajectory.map(function (t) {
              return t.offered;
            }),
            backgroundColor: colors.series[0]
          },
          {
            label: "Accepted",
            data: payload.trajectory.map(function (t) {
              return t.accepted;
            }),
            backgroundColor: colors.good
          }
        ]
      },
      options: options
    };
  };

  builders["client-rate"] = function (payload) {
    var colors = TBK.palette();
    var options = baseOptions(percentAxis(colors.grid), colors.grid);
    options.plugins.tooltip.callbacks = {
      title: function (items) {
        return payload.trajectory[items[0].dataIndex].date;
      },
      label: function (item) {
        var row = payload.trajectory[item.dataIndex];
        return "Rate: " + pct(item.raw) + "  (" + row.accepted + "/" + row.offered + ")";
      }
    };
    return {
      type: "line",
      data: {
        labels: payload.trajectory.map(function (t) {
          return t.date.slice(5);
        }),
        datasets: [
          {
            label: "Acceptance rate",
            data: payload.trajectory.map(function (t) {
              return t.rate;
            }),
            borderColor: colors.series[0],
            borderWidth: 2.5,
            pointRadius: 3
          }
        ]
      },
      options: options
    };
  };

  /* ------------------------------------------------------- missed work */

  /* Missed jobs by cause. One series, so no legend -- the card title names it.
     Counts, never money: offerAmount is empty on every record this account
     produces, which is why the page says so above the chart. */
  builders["missed-causes"] = function (payload) {
    var colors = TBK.palette();
    var rows = payload.causes || [];
    var options = baseOptions(countAxis(colors.grid), colors.grid);
    options.plugins.tooltip.callbacks = {
      title: function (items) {
        return items[0].label;
      },
      label: function (item) {
        var row = rows[item.dataIndex] || {};
        return (
          "Missed: " + row.missed + "  (" + pct(row.share) + " of all missed jobs)"
        );
      }
    };
    return {
      type: "bar",
      data: {
        labels: rows.map(function (r) {
          return String(r.cause || "").replace(/_/g, " ");
        }),
        datasets: [
          {
            label: "Missed",
            data: rows.map(function (r) {
              return r.missed;
            }),
            backgroundColor: colors.critical,
            borderRadius: 4
          }
        ]
      },
      options: options
    };
  };

  /* Share of offers that went unanswered, per hour of day, against the
     blind-spot threshold. The threshold is a second dataset rather than an
     annotation so it survives being handed to the real Chart.js unchanged.

     A null bar is a genuine gap -- an hour with no offers has no miss rate,
     and drawing it as 0% would report a quiet hour as perfect coverage. */
  builders["blind-hours"] = function (payload) {
    var colors = TBK.palette();
    var rows = payload.by_hour || [];
    var threshold = payload.threshold;
    var options = baseOptions(percentAxis(colors.grid), colors.grid);
    options.plugins.tooltip.callbacks = {
      title: function (items) {
        return items[0].label + ":00";
      },
      label: function (item) {
        if (item.datasetIndex === 1) return "Threshold: " + pct(threshold);
        var row = rows[item.dataIndex] || {};
        if (!row.offers) return "No offers in this hour";
        return (
          "Unanswered: " +
          row.no_response +
          " of " +
          row.offers +
          "  (" +
          pct(row.no_response_rate) +
          ")"
        );
      }
    };
    var labels = rows.map(function (r) {
      return String(r.label || "").slice(0, 2);
    });
    return {
      type: "bar",
      data: {
        labels: labels,
        datasets: [
          {
            label: "Share unanswered",
            data: rows.map(function (r) {
              return r.no_response_rate;
            }),
            backgroundColor: colors.series[0],
            borderRadius: 4
          },
          {
            type: "line",
            label: "Blind-spot threshold",
            data: labels.map(function () {
              return threshold === null || threshold === undefined ? null : threshold;
            }),
            borderColor: colors.critical,
            borderWidth: 2,
            borderDash: [5, 4],
            pointRadius: 0
          }
        ]
      },
      options: options
    };
  };

  /* Close-off candidates: what was offered against what we actually took. */
  builders["closeoff-types"] = function (payload) {
    var colors = TBK.palette();
    var rows = payload.types || [];
    var options = baseOptions(countAxis(colors.grid), colors.grid);
    options.plugins.tooltip.callbacks = {
      title: function (items) {
        return (rows[items[0].dataIndex] || {}).service_type_raw || items[0].label;
      },
      label: function (item) {
        var row = rows[item.dataIndex] || {};
        if (item.datasetIndex === 0) return "Offered: " + row.offers;
        return "Accepted: " + row.accepted + "  (" + pct(row.rate) + ")";
      }
    };
    return {
      type: "bar",
      data: {
        labels: rows.map(function (r) {
          var name = String(r.service_type_raw || "");
          return name.length > 18 ? name.slice(0, 17) + "…" : name;
        }),
        datasets: [
          {
            label: "Offered",
            data: rows.map(function (r) {
              return r.offers;
            }),
            backgroundColor: colors.series[1],
            borderRadius: 4
          },
          {
            label: "Accepted",
            data: rows.map(function (r) {
              return r.accepted;
            }),
            backgroundColor: colors.good,
            borderRadius: 4
          }
        ]
      },
      options: options
    };
  };

  /* ---------------------------------------------------------- chart mount */

  function mountCharts(root) {
    if (!global.Chart) return;
    var scope = root && root.querySelectorAll ? root : document;
    var canvases = scope.querySelectorAll("canvas[data-chart]");
    for (var i = 0; i < canvases.length; i++) {
      var canvas = canvases[i];
      if (canvas.__tbkChart) continue;
      var name = canvas.getAttribute("data-chart");
      var builder = builders[name];
      if (!builder) continue;
      var payload;
      try {
        payload = JSON.parse(canvas.getAttribute("data-chart-data") || "{}");
      } catch (err) {
        continue;
      }
      try {
        var config = builder(payload);
        canvas.__tbkChart = new global.Chart(canvas, config);
        canvas.__tbkPayload = payload;
        canvas.__tbkName = name;
        built.push(canvas);
      } catch (err) {
        /* One broken chart must not take the page down. */
        if (global.console) global.console.error("chart '" + name + "' failed:", err);
      }
    }
  }

  TBK.rebuildCharts = function () {
    var canvases = built.slice();
    built = [];
    for (var i = 0; i < canvases.length; i++) {
      var canvas = canvases[i];
      if (canvas.__tbkChart) {
        canvas.__tbkChart.destroy();
        canvas.__tbkChart = null;
      }
    }
    mountCharts(document);
  };

  TBK.mountCharts = mountCharts;

  /* ------------------------------------------------------ heatmap tooltip */

  var floating = null;

  function floatingTip() {
    if (!floating) {
      floating = document.createElement("div");
      floating.className = "tbk-tooltip";
      floating.style.position = "fixed";
      floating.style.whiteSpace = "pre-line";
      document.body.appendChild(floating);
    }
    return floating;
  }

  function wireTips() {
    document.addEventListener("mouseover", function (event) {
      var el = event.target.closest ? event.target.closest("[data-tip]") : null;
      if (!el) return;
      var tip = floatingTip();
      /* textContent, never innerHTML: the tip text comes from the database
         (client names, service strings) and must never be parsed as markup. */
      tip.textContent = el.getAttribute("data-tip");
      tip.classList.add("on");
      var rect = el.getBoundingClientRect();
      tip.style.left = rect.left + rect.width / 2 + "px";
      tip.style.top = rect.top - 6 + "px";
    });
    document.addEventListener("mouseout", function (event) {
      var el = event.target.closest ? event.target.closest("[data-tip]") : null;
      if (el && floating) floating.classList.remove("on");
    });
    global.addEventListener("scroll", function () {
      if (floating) floating.classList.remove("on");
    }, true);
  }

  /* ------------------------------------------------------- confirm dialogs */

  function wireConfirms() {
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!form.matches || !form.matches("[data-confirm]")) return;
      if (!global.confirm(form.getAttribute("data-confirm"))) {
        event.preventDefault();
      }
    });
  }

  /* ---------------------------------------------------------- copy button */

  /* The close-off notes are meant to be pasted into an email. The button is an
     accelerator only: the <pre> carries `user-select: all`, so one click
     selects the whole note and Ctrl+C still works with JavaScript disabled,
     over plain HTTP (where navigator.clipboard is unavailable), or if the
     permission is refused. Nothing here is the only route to the text. */
  function selectAll(node) {
    try {
      var range = document.createRange();
      range.selectNodeContents(node);
      var selection = global.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      return true;
    } catch (err) {
      return false;
    }
  }

  function flash(button, text) {
    var original = button.getAttribute("data-label") || button.textContent;
    button.setAttribute("data-label", original);
    button.textContent = text;
    var block = button.closest ? button.closest(".copyblock") : null;
    if (block) block.classList.add("copied");
    global.setTimeout(function () {
      button.textContent = original;
      if (block) block.classList.remove("copied");
    }, 1600);
  }

  function wireCopy() {
    document.addEventListener("click", function (event) {
      var button = event.target.closest ? event.target.closest("[data-copy]") : null;
      if (!button) return;
      var node = document.querySelector(button.getAttribute("data-copy"));
      if (!node) return;
      event.preventDefault();

      var text = node.textContent || "";
      if (global.navigator && global.navigator.clipboard && global.navigator.clipboard.writeText) {
        global.navigator.clipboard.writeText(text).then(
          function () {
            flash(button, "Copied");
          },
          function () {
            flash(button, selectAll(node) ? "Selected — press Ctrl+C" : "Copy failed");
          }
        );
        return;
      }
      flash(button, selectAll(node) ? "Selected — press Ctrl+C" : "Copy failed");
    });
  }

  /* ------------------------------------------------------------------ init */

  applyTheme(
    (function () {
      try {
        return global.localStorage.getItem(THEME_KEY);
      } catch (err) {
        return null;
      }
    })()
  );

  function init() {
    updateToggleLabel();
    var toggle = document.getElementById("theme-toggle");
    if (toggle) toggle.addEventListener("click", TBK.toggleTheme);
    wireTips();
    wireConfirms();
    wireCopy();
    mountCharts(document);
    document.body.addEventListener("htmx:afterSwap", function (event) {
      mountCharts(event.target);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  global.TBK = TBK;
})(window);
