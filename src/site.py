"""Rendering and history for the ClinePass models report.

This module owns everything that turns a snapshot into the published static site
(dashboard, methodology guide, archive index, dated reports) plus the snapshot
history files themselves. The site is self-contained: inline CSS and JavaScript,
no CDN, no external assets, so an offline run renders the same pages as a live one
apart from the mode badge.
"""

from __future__ import annotations

import html
import json
import math
from datetime import date, datetime
from pathlib import Path

from . import monitor

ARCHIVE_FILE = "archive.json"
SNAPSHOT_SUFFIX_OFFLINE = ".offline.json"


def escape(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def fmt_usd(value, digits=4):
    """Format a USD amount, or the documented unavailable marker."""
    number = monitor.as_float(value)
    if number is None:
        return "n/a"
    if number == 0:
        return "$0"
    if abs(number) < 10 ** (-digits):
        return f"&lt;${10 ** (-digits):.{digits}f}"
    return f"${number:,.{digits}f}"


def fmt_usd_mtok(value, digits=4):
    """Format a per-million-token price without trailing zeros."""
    number = monitor.as_float(value)
    if number is None:
        return "n/a"
    if number == 0:
        return "$0"
    text = f"{number:,.{digits}f}".rstrip("0").rstrip(".")
    return f"${text}"


def fmt_points(value, digits=2):
    number = monitor.as_float(value)
    return "n/a" if number is None else f"{number:,.{digits}f}"


def fmt_int(value):
    number = monitor.as_float(value)
    return "n/a" if number is None else f"{int(number):,}"


PROVENANCE_LABELS = {
    monitor.PROVENANCE_MEASURED: ("measured", "prov-measured"),
    monitor.PROVENANCE_REFERENCE: ("reference", "prov-reference"),
    monitor.PROVENANCE_CATALOG_FREE: ("free", "prov-free"),
    monitor.PROVENANCE_INPUT_FALLBACK: ("input fallback", "prov-fallback"),
}


def provenance_label(rates, rate_class):
    provenance = monitor.rate_provenance(rates, rate_class)
    return PROVENANCE_LABELS.get(provenance, ("unavailable", "prov-none"))


def model_rate_source(model):
    """One label describing where this model's rates came from."""
    provenances = {
        monitor.rate_provenance(model.get("rates"), rate_class)
        for rate_class in monitor.FIT_CLASSES
    }
    provenances.discard(None)
    if not provenances:
        return "unavailable", "prov-none"
    if provenances == {monitor.PROVENANCE_CATALOG_FREE}:
        return "free (catalog)", "prov-free"
    if provenances == {monitor.PROVENANCE_MEASURED}:
        return "measured", "prov-measured"
    if provenances == {monitor.PROVENANCE_REFERENCE}:
        return "reference", "prov-reference"
    return "mixed", "prov-mixed"


def rate_window_text(model):
    """The window and row count behind a measured rate, for the page."""
    for rate_class in monitor.FIT_CLASSES:
        entry = (model.get("rates") or {}).get(rate_class) or {}
        if entry.get("provenance") == monitor.PROVENANCE_MEASURED:
            return (
                f"{entry.get('window_start')} to {entry.get('window_end')} "
                f"({entry.get('rows')} rows)"
            )
    return ""


CSS = """
:root {
  color-scheme: light dark;
  --bg: #f7f8fa; --card: #ffffff; --ink: #1b1f24; --muted: #5b6472;
  --line: #e2e5ea; --accent: #0b6bcb; --good: #1a7f37; --warn: #9a6700; --bad: #b3261e;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #10141a; --card: #171c24; --ink: #e6e9ee; --muted: #9aa4b2;
          --line: #2a313b; --accent: #4c9ffe; --good: #4ac26b; --warn: #d4a72c; --bad: #f85149; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.55 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
.wrap { max-width: 1200px; margin: 0 auto; padding: 28px 20px 60px; }
h1 { font-size: 26px; margin: 0 0 6px; }
h2 { font-size: 19px; margin: 34px 0 10px; }
h3 { font-size: 16px; margin: 22px 0 8px; }
a { color: var(--accent); }
.sub { color: var(--muted); margin: 0 0 16px; }
.badges { display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0 20px; }
.badge { display: inline-block; padding: 3px 9px; border-radius: 999px; font-size: 12px;
  border: 1px solid var(--line); background: var(--card); color: var(--muted); }
.badge.live { color: var(--good); border-color: var(--good); font-weight: 600; }
.badge.offline { color: var(--warn); border-color: var(--warn); font-weight: 600; }
.badge.fallback { color: var(--bad); border-color: var(--bad); font-weight: 600; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 12px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
.card .k { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
.card .v { font-size: 20px; font-weight: 600; margin-top: 2px; }
.card .n { color: var(--muted); font-size: 12px; margin-top: 2px; }
.panel { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 14px 16px; margin-top: 12px; }
.panel.warnings { border-left: 3px solid var(--warn); }
.panel.warnings li { color: var(--warn); }
.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 14px 0 8px; }
input, select, button { font: inherit; color: var(--ink); background: var(--card);
  border: 1px solid var(--line); border-radius: 8px; padding: 6px 9px; }
button.quick[aria-pressed="true"] { border-color: var(--accent); color: var(--accent); font-weight: 600; }
table { width: 100%; border-collapse: collapse; background: var(--card); font-size: 13.5px; }
th, td { border-bottom: 1px solid var(--line); padding: 7px 8px; text-align: left; vertical-align: top; }
th { position: sticky; top: 0; background: var(--card); cursor: pointer; white-space: nowrap; }
th .dir { color: var(--muted); font-size: 11px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.filters th { cursor: default; padding: 4px; }
tr.filters input { width: 100%; padding: 4px 6px; font-size: 12px; }
.tag { display: inline-block; padding: 1px 6px; border-radius: 6px; font-size: 11px;
  border: 1px solid var(--line); color: var(--muted); }
.prov-measured { color: var(--good); border-color: var(--good); }
.prov-reference { color: var(--warn); border-color: var(--warn); }
.prov-free { color: var(--accent); border-color: var(--accent); }
.prov-fallback, .prov-none, .prov-mixed { color: var(--muted); }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12.5px; }
.chart { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px; }
.chart svg { width: 100%; height: auto; display: block; }
.chart text { fill: var(--ink); font-size: 11px; }
.chart .axis { stroke: var(--line); }
.chart .grid { stroke: var(--line); stroke-dasharray: 3 4; opacity: .7; }
.chart .frontier { fill: none; stroke: var(--accent); stroke-width: 2; }
.chart .point { fill: var(--accent); }
.chart .point-free { fill: var(--warn); }
.chart .band { fill: var(--bg); stroke: var(--line); stroke-dasharray: 4 4; }
.chart .legend-box { fill: var(--card); stroke: var(--line); }
footer { margin-top: 40px; color: var(--muted); font-size: 12px; }
ul.links { padding-left: 18px; }
""".strip()

CHART_WIDTH = 940
CHART_HEIGHT = 470
CHART_LEFT = 78
CHART_RIGHT = 918
CHART_TOP = 26
CHART_BOTTOM = 396
FREE_BAND_LEFT = 12
FREE_BAND_RIGHT = 62
LOG_TICK_CANDIDATES = (
    0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5,
    1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0,
)


def _log_ticks(low, high, count=6):
    inside = [value for value in LOG_TICK_CANDIDATES if low <= value <= high]
    if len(inside) <= count:
        return inside
    step = len(inside) / float(count)
    ticks = []
    for index in range(count):
        candidate = inside[int(index * step)]
        if candidate not in ticks:
            ticks.append(candidate)
    return ticks


def _quality_ticks(low, high, count=5):
    if high <= low:
        return [low]
    step = (high - low) / float(count - 1)
    return [round(low + step * index, 1) for index in range(count)]


def render_chart(report, config=None):
    """Render the combined cost/quality chart as inline SVG.

    One point per eligible model, a single non-dominated frontier over the plotted
    average cost, an annotation for every frontier model, and a legend inside the
    lower-right of the plot area. Catalog-free models are drawn in a band at the
    left because a zero cost has no place on a logarithmic axis.
    """
    chart = report.get("chart") or {}
    points = list(chart.get("points") or [])
    band = list(chart.get("free_band") or [])
    if not points and not band:
        return (
            '<svg viewBox="0 0 940 200" role="img" aria-label="Cost and quality chart">'
            '<text x="20" y="40" data-role="chart-empty">No model has both a current AA quality '
            "score and a usable cost, so there is nothing to plot.</text></svg>"
        )

    costs = [point["cost"] for point in points] or [1.0]
    low = min(costs) * 0.55
    high = max(costs) * 1.7
    qualities = [point["quality"] for point in points + band]
    q_low = max(0.0, min(qualities) - 5.0)
    q_high = min(100.0, max(qualities) + 5.0)
    if q_high <= q_low:
        q_high = q_low + 1.0
    span = math.log10(high) - math.log10(low)

    def x_pos(cost):
        value = max(cost, low)
        ratio = 0.0 if span == 0 else (math.log10(value) - math.log10(low)) / span
        return CHART_LEFT + ratio * (CHART_RIGHT - CHART_LEFT)

    def y_pos(quality):
        ratio = (quality - q_low) / (q_high - q_low)
        return CHART_BOTTOM - ratio * (CHART_BOTTOM - CHART_TOP)

    parts = [
        '<svg viewBox="0 0 %d %d" role="img" aria-label="Model quality against task cost">'
        % (CHART_WIDTH, CHART_HEIGHT)
    ]
    for tick in _quality_ticks(q_low, q_high):
        y = y_pos(tick)
        parts.append(
            '<line class="grid" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>'
            % (CHART_LEFT, y, CHART_RIGHT, y)
        )
        parts.append(
            '<text x="%d" y="%.1f" text-anchor="end">%.1f</text>' % (CHART_LEFT - 8, y + 4, tick)
        )
    for tick in _log_ticks(low, high):
        x = x_pos(tick)
        parts.append(
            '<line class="grid" x1="%.1f" y1="%d" x2="%.1f" y2="%d"/>'
            % (x, CHART_TOP, x, CHART_BOTTOM)
        )
        parts.append(
            '<text x="%.1f" y="%d" text-anchor="middle">%g</text>' % (x, CHART_BOTTOM + 18, tick)
        )
    parts.append(
        '<line class="axis" x1="%d" y1="%d" x2="%d" y2="%d"/>'
        % (CHART_LEFT, CHART_BOTTOM, CHART_RIGHT, CHART_BOTTOM)
    )
    parts.append(
        '<line class="axis" x1="%d" y1="%d" x2="%d" y2="%d"/>'
        % (CHART_LEFT, CHART_TOP, CHART_LEFT, CHART_BOTTOM)
    )
    parts.append(
        '<text x="%d" y="%d" text-anchor="middle">Mean of available planning and execution task '
        "cost (USD, log scale)</text>"
        % ((CHART_LEFT + CHART_RIGHT) // 2, CHART_BOTTOM + 40)
    )
    parts.append(
        '<text transform="translate(18,%d) rotate(-90)" text-anchor="middle">AA Intelligence '
        "Index</text>" % ((CHART_TOP + CHART_BOTTOM) // 2)
    )

    parts.append(_chart_series(points, band, x_pos, y_pos))
    parts.append("</svg>")
    return "".join(parts)


def _chart_series(points, band, x_pos, y_pos):
    """Marks, frontier line, annotations, the free band, and the in-plot legend."""
    parts = []
    if band:
        parts.append(
            '<rect class="band" x="%d" y="%d" width="%d" height="%d" data-role="free-band"/>'
            % (
                FREE_BAND_LEFT,
                CHART_TOP,
                FREE_BAND_RIGHT - FREE_BAND_LEFT,
                CHART_BOTTOM - CHART_TOP,
            )
        )
        parts.append(
            '<text x="%d" y="%d" text-anchor="middle" data-role="free-band-label">$0 free</text>'
            % ((FREE_BAND_LEFT + FREE_BAND_RIGHT) // 2, CHART_TOP - 10)
        )

    frontier_points = sorted(
        (point for point in points if point.get("frontier")), key=lambda point: point["cost"]
    )
    if len(frontier_points) > 1:
        coordinates = " ".join(
            "%.1f,%.1f" % (x_pos(point["cost"]), y_pos(point["quality"]))
            for point in frontier_points
        )
        parts.append('<polyline class="frontier" data-role="frontier" points="%s"/>' % coordinates)

    for index, point in enumerate(points):
        x = x_pos(point["cost"])
        y = y_pos(point["quality"])
        parts.append(
            '<g data-role="point" data-model-id="%s" data-frontier="%s" data-free="false">'
            % (escape(point["id"]), "true" if point.get("frontier") else "false")
        )
        parts.append(
            '<circle class="point" cx="%.1f" cy="%.1f" r="5"><title>%s: quality %s, mean cost %s'
            "</title></circle>"
            % (x, y, escape(point["name"]), point["quality"], fmt_usd(point["cost"]))
        )
        parts.append("</g>")
        if point.get("frontier"):
            label_y = y + (16 if index % 2 == 0 else -10)
            anchor = "start" if x < (CHART_LEFT + CHART_RIGHT) / 2 else "end"
            offset = 9 if anchor == "start" else -9
            parts.append(
                '<text data-role="annotation" data-model-id="%s" x="%.1f" y="%.1f" '
                'text-anchor="%s">%s</text>'
                % (
                    escape(point["id"]),
                    x + offset,
                    label_y,
                    anchor,
                    escape(point["name"]),
                )
            )

    for point in band:
        x = (FREE_BAND_LEFT + FREE_BAND_RIGHT) / 2.0
        y = y_pos(point["quality"])
        parts.append(
            '<g data-role="point" data-model-id="%s" data-frontier="%s" data-free="true">'
            % (escape(point["id"]), "true" if point.get("frontier") else "false")
        )
        parts.append(
            '<circle class="point point-free" cx="%.1f" cy="%.1f" r="5"><title>%s: quality %s, '
            "$0 (free) - not on the log axis</title></circle>"
            % (x, y, escape(point["name"]), point["quality"])
        )
        parts.append("</g>")
        parts.append(
            '<text data-role="annotation" data-model-id="%s" x="%.1f" y="%.1f" '
            'text-anchor="middle">$0 (free)</text>' % (escape(point["id"]), x, y - 10)
        )

    legend_x = CHART_RIGHT - 186
    legend_y = CHART_BOTTOM - 60
    parts.append(
        '<g data-role="legend" data-x="%d" data-y="%d" transform="translate(%d,%d)">'
        % (legend_x, legend_y, legend_x, legend_y)
    )
    parts.append('<rect class="legend-box" width="176" height="50" rx="6"/>')
    parts.append('<circle class="point" cx="14" cy="15" r="5"/>')
    parts.append('<text x="26" y="19">eligible model</text>')
    parts.append('<line class="frontier" x1="6" y1="31" x2="22" y2="31"/>')
    parts.append('<text x="26" y="35">non-dominated frontier</text>')
    parts.append('<circle class="point point-free" cx="14" cy="46" r="5"/>')
    parts.append('<text x="26" y="50">$0 free (off the log axis)</text>')
    parts.append("</g>")
    parts.append(
        '<text x="%d" y="%d" data-role="chart-note">Free models carry a zero task cost, which '
        "cannot be shown on a logarithmic axis, so they are drawn in the left band and stay in the "
        "non-dominated set.</text>" % (CHART_LEFT, CHART_HEIGHT - 10)
    )
    return "".join(parts)


TABLE_COLUMNS = (
    ("model", "Model / family", "text"),
    ("source", "Rate source", "text"),
    ("quality", "AA quality", "number"),
    ("coding", "Coding index (display only)", "number"),
    ("input", "Input $/1M", "number"),
    ("output", "Output $/1M", "number"),
    ("cacheread", "Cache read $/1M", "number"),
    ("cachewrite", "Cache write $/1M", "number"),
    ("costplanning", "Planning cost", "number"),
    ("effplanning", "Planning efficiency", "number"),
    ("costexecution", "Execution cost", "number"),
    ("effexecution", "Execution efficiency", "number"),
    ("evidence", "AA evidence", "text"),
)


def _effective(efficiency_value, cost, free):
    if efficiency_value is None:
        if free and cost == 0:
            return '<span class="tag prov-free">free</span>'
        return '<span class="muted">n/a</span>'
    return fmt_points(efficiency_value)


def _rate_cell(model, rate_class):
    rates = model.get("rates") or {}
    label, css = provenance_label(rates, rate_class)
    value = monitor.rate_value(rates, rate_class)
    return '<td class="num" style="white-space:nowrap">%s<br/><span class="tag %s">%s</span></td>' % (
        fmt_usd_mtok(value),
        css,
        escape(label),
    )


def _model_row(model):
    """One table row, with the values mirrored into data attributes for filtering."""
    source_label, source_css = model_rate_source(model)
    quality_record = model.get("quality") or {}
    quality = quality_record.get("intelligence_index")
    coding = quality_record.get("coding_index")
    rates = model.get("rates") or {}
    costs = model.get("cost_usd") or {}
    efficiencies = model.get("efficiency") or {}
    measured = any(
        monitor.rate_provenance(rates, rate_class) == monitor.PROVENANCE_MEASURED
        for rate_class in monitor.FIT_CLASSES
    )

    model_cell = '%s <span class="tag">%s</span>' % (
        escape(model["name"]),
        escape(model["family"]),
    )
    model_cell += '<br/><span class="muted small code">%s</span>' % escape(model["id"])
    if model.get("context_window"):
        model_cell += '<span class="muted small"> - context %s</span>' % fmt_int(
            model["context_window"]
        )
    window = rate_window_text(model)
    if window:
        model_cell += '<br/><span class="muted small">rate window %s</span>' % escape(window)

    if quality_record:
        evidence = '%s<br/><span class="muted small">retrieved %s</span>' % (
            escape(quality_record.get("aa_name") or "-"),
            escape(str(quality_record.get("retrieved_at") or "")[:10]),
        )
    else:
        evidence = '<span class="muted">no current AA score</span>'

    def numeric(value):
        return "" if value is None else value

    attributes = {
        "model": (model["name"] + " " + model["id"]).lower(),
        "family": model["family"],
        "source": source_label,
        "quality": numeric(quality),
        "coding": numeric(coding),
        "input": numeric(monitor.rate_value(rates, "input")),
        "output": numeric(monitor.rate_value(rates, "output")),
        "cacheread": numeric(monitor.rate_value(rates, "cache_read")),
        "cachewrite": numeric(monitor.rate_value(rates, "cache_write")),
        "costplanning": numeric(costs.get("planning")),
        "costexecution": numeric(costs.get("execution")),
        "effplanning": numeric(efficiencies.get("planning")),
        "effexecution": numeric(efficiencies.get("execution")),
        "evidence": quality_record.get("aa_name") or "no score",
        "benchmarked": "1" if quality_record else "0",
        "priced": "1" if model.get("priced") else "0",
        "free": "1" if model.get("free") else "0",
        "measured": "1" if measured else "0",
    }
    attribute_text = " ".join(
        'data-%s="%s"' % (key, escape(value)) for key, value in attributes.items()
    )
    return (
        "<tr %s>"
        "<td>%s</td>"
        '<td><span class="tag %s">%s</span></td>'
        '<td class="num">%s</td>'
        '<td class="num">%s</td>'
        "%s%s%s%s"
        '<td class="num">%s</td>'
        '<td class="num">%s</td>'
        '<td class="num">%s</td>'
        '<td class="num">%s</td>'
        "<td>%s</td>"
        "</tr>"
    ) % (
        attribute_text,
        model_cell,
        source_css,
        escape(source_label),
        fmt_points(quality),
        fmt_points(coding),
        _rate_cell(model, "input"),
        _rate_cell(model, "output"),
        _rate_cell(model, "cache_read"),
        _rate_cell(model, "cache_write"),
        fmt_usd(costs.get("planning")),
        _effective(efficiencies.get("planning"), costs.get("planning"), model.get("free")),
        fmt_usd(costs.get("execution")),
        _effective(efficiencies.get("execution"), costs.get("execution"), model.get("free")),
        evidence,
    )


def render_table(report, config=None):
    """The sortable, filterable model table with per-column filters."""
    models = report.get("models") or []
    header = ["<tr>"]
    for key, label, sort_type in TABLE_COLUMNS:
        header.append(
            '<th data-key="%s" data-sort="%s">%s <span class="dir"></span></th>'
            % (key, sort_type, escape(label))
        )
    header.append("</tr>")
    filters = ['<tr class="filters">']
    for key, label, sort_type in TABLE_COLUMNS:
        filters.append(
            '<th><input data-filter="%s" aria-label="Filter by %s" placeholder="%s"/></th>'
            % (key, escape(label), "number" if sort_type == "number" else "text")
        )
    filters.append("</tr>")

    families = sorted({model["family"] for model in models})
    controls = [
        '<div class="controls">',
        '<input id="model-search" type="search" placeholder="Search models" '
        'aria-label="Search models"/>',
        '<select id="family-filter" aria-label="Family filter"><option value="">All families'
        "</option>",
    ]
    for family in families:
        controls.append('<option value="%s">%s</option>' % (escape(family), escape(family)))
    controls.append("</select>")
    for quick, label in (
        ("benchmarked-priced", "Benchmarked &amp; Priced"),
        ("measured-rates", "Measured rates"),
        ("free", "Free models"),
    ):
        controls.append(
            '<button type="button" class="quick" data-quick="%s" aria-pressed="false">%s</button>'
            % (quick, label)
        )
    controls.append('<span id="row-count" class="muted small"></span>')
    controls.append("</div>")

    return "".join(
        controls
        + [
            '<table id="models"><thead>',
            *header,
            *filters,
            "</thead><tbody>",
            *[_model_row(model) for model in models],
            "</tbody></table>",
        ]
    )


TABLE_SCRIPT = """
(function () {
  var table = document.getElementById('models');
  if (!table) { return; }
  var rows = Array.prototype.slice.call(table.tBodies[0].rows);
  var filters = Array.prototype.slice.call(table.querySelectorAll('tr.filters input'));
  var quicks = Array.prototype.slice.call(document.querySelectorAll('button.quick'));
  var search = document.getElementById('model-search');
  var family = document.getElementById('family-filter');
  var counter = document.getElementById('row-count');
  var sortKey = null;
  var sortDir = 1;

  function matches(value, spec) {
    spec = (spec || '').trim();
    if (!spec) { return true; }
    var m = spec.match(/^(>=|<=|>|<|=)?\\s*(-?\\d+(?:\\.\\d+)?)$/);
    if (m && value !== '') {
      var target = parseFloat(m[2]);
      var actual = parseFloat(value);
      if (!isNaN(actual)) {
        if (m[1] === '>') { return actual > target; }
        if (m[1] === '>=') { return actual >= target; }
        if (m[1] === '<') { return actual < target; }
        if (m[1] === '<=') { return actual <= target; }
        return actual === target;
      }
    }
    return value.toLowerCase().indexOf(spec.toLowerCase()) !== -1;
  }

  function apply() {
    var active = quicks.filter(function (button) {
      return button.getAttribute('aria-pressed') === 'true';
    }).map(function (button) { return button.dataset.quick; });
    var term = search ? search.value.trim().toLowerCase() : '';
    var chosenFamily = family ? family.value : '';
    rows.forEach(function (row) {
      var visible = true;
      filters.forEach(function (input) {
        if (visible && !matches(row.dataset[input.dataset.filter] || '', input.value)) {
          visible = false;
        }
      });
      if (visible && term && (row.dataset.model || '').indexOf(term) === -1) { visible = false; }
      if (visible && chosenFamily && row.dataset.family !== chosenFamily) { visible = false; }
      if (visible) {
        active.forEach(function (name) {
          if (name === 'benchmarked-priced' &&
              !(row.dataset.benchmarked === '1' && row.dataset.priced === '1')) { visible = false; }
          if (name === 'measured-rates' && row.dataset.measured !== '1') { visible = false; }
          if (name === 'free' && row.dataset.free !== '1') { visible = false; }
        });
      }
      row.hidden = !visible;
    });
    if (counter) {
      var shown = rows.filter(function (row) { return !row.hidden; }).length;
      counter.textContent = shown + ' of ' + rows.length + ' models shown';
    }
  }

  Array.prototype.slice.call(table.tHead.querySelectorAll('th[data-key]')).forEach(function (th) {
    th.addEventListener('click', function () {
      var key = th.dataset.key;
      sortDir = sortKey === key ? -sortDir : 1;
      sortKey = key;
      var numeric = th.dataset.sort === 'number';
      rows.sort(function (a, b) {
        var av = a.dataset[key] || '';
        var bv = b.dataset[key] || '';
        if (numeric) {
          var an = av === '' ? Infinity : parseFloat(av);
          var bn = bv === '' ? Infinity : parseFloat(bv);
          return (an - bn) * sortDir;
        }
        return av.localeCompare(bv) * sortDir;
      });
      rows.forEach(function (row) { table.tBodies[0].appendChild(row); });
      Array.prototype.slice.call(table.tHead.querySelectorAll('th[data-key]')).forEach(function (other) {
        var marker = other.querySelector('.dir');
        if (marker) { marker.textContent = other === th ? (sortDir === 1 ? 'asc' : 'desc') : ''; }
      });
    });
  });

  filters.forEach(function (input) { input.addEventListener('input', apply); });
  quicks.forEach(function (button) {
    button.addEventListener('click', function () {
      var pressed = button.getAttribute('aria-pressed') === 'true';
      button.setAttribute('aria-pressed', pressed ? 'false' : 'true');
      apply();
    });
  });
  if (search) { search.addEventListener('input', apply); }
  if (family) { family.addEventListener('change', apply); }
  apply();
})();
"""


def page(title, body, script="", footer=""):
    """Wrap page content in the shared document shell."""
    script_tag = "<script>%s</script>" % script if script else ""
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8"/>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>\n'
        "<title>%s</title>\n<style>\n%s\n</style>\n</head>\n<body>\n<div class=\"wrap\">\n"
        "%s\n%s\n</div>\n%s</body>\n</html>\n"
    ) % (escape(title), CSS, body, script_tag, footer)


def _card(key, value, note=""):
    return (
        '<div class="card"><div class="k">%s</div><div class="v">%s</div>'
        '<div class="n">%s</div></div>' % (escape(key), value, escape(note))
    )


def _badges(snapshot, config):
    report_meta = snapshot.get("report") or {}
    mode = report_meta.get("mode")
    badges = []
    if mode == monitor.MODE_OFFLINE:
        badges.append(
            '<span class="badge offline">OFFLINE (fixtures - not a live evaluation)</span>'
        )
    else:
        badges.append('<span class="badge live">LIVE</span>')
    catalog = report_meta.get("catalog") or {}
    if catalog.get("source") == monitor.CATALOG_SOURCE_FALLBACK:
        badges.append(
            '<span class="badge fallback">catalog fallback (recommended-models unavailable)</span>'
        )
    else:
        badges.append(
            '<span class="badge">catalog: %s</span>' % escape(catalog.get("source") or "unknown")
        )
    narrative = report_meta.get("narrative") or {}
    badges.append(
        '<span class="badge">summary: %s</span>' % escape(narrative.get("model") or "deterministic")
    )
    window = report_meta.get("rate_window") or {}
    badges.append(
        '<span class="badge">rate window: %s to %s (%s days)</span>'
        % (escape(window.get("start")), escape(window.get("end")), escape(window.get("days")))
    )
    reference = snapshot.get("reference_rates") or {}
    badges.append('<span class="badge">fallback rates: %s</span>' % escape(reference.get("source_name")))
    return '<div class="badges">%s</div>' % "".join(badges)


def _best_value_card(label, winner, profile_label):
    if not winner:
        return _card(label, '<span class="muted">n/a</span>', "no eligible Pareto model")
    return _card(
        label,
        escape(winner.get("name")),
        "%s: %s points, task cost %s, quality %s"
        % (
            profile_label,
            fmt_points(winner.get("efficiency")),
            fmt_usd(winner.get("cost_usd")),
            fmt_points(winner.get("quality"), 1),
        ),
    )


def _stat_cards(snapshot, config):
    stats = snapshot.get("stats") or {}
    profiles = (config.get("profiles") or {})
    medians = snapshot.get("medians") or {}
    best = snapshot.get("best_value") or {}
    cards = [
        _card("Models discovered", str(stats.get("discovered", 0)), "ClinePass catalog plus free models"),
        _card(
            "Priced / benchmarked",
            "%s / %s" % (stats.get("priced", 0), stats.get("benchmarked", 0)),
            "usable rates / current AA score",
        ),
        _card(
            "Measured rates",
            str(stats.get("measured_rates", 0)),
            "fitted from this account's billing usage",
        ),
        _card("Free models", str(stats.get("free_models", 0)), "catalog-declared $0"),
    ]
    highest = stats.get("highest_quality")
    cards.append(
        _card(
            "Highest quality",
            escape(highest.get("name")) if highest else '<span class="muted">n/a</span>',
            "AA Intelligence Index %s" % fmt_points((highest or {}).get("quality"), 1),
        )
    )
    for name in profiles:
        label = profiles[name].get("label") or name
        cards.append(
            _best_value_card("%s best value" % label, best.get(name), label)
        )
    cheapest = stats.get("lowest_cost")
    cards.append(
        _card(
            "Lowest cost",
            escape(cheapest.get("name")) if cheapest else '<span class="muted">n/a</span>',
            "lowest profile task cost %s%s"
            % (
                fmt_usd((cheapest or {}).get("cost_usd")),
                " (free)" if (cheapest or {}).get("free") else "",
            ),
        )
    )
    for name in profiles:
        label = profiles[name].get("label") or name
        cards.append(
            _card("Median %s cost" % label.lower(), fmt_usd(medians.get(name)), "feeds the efficiency penalty")
        )
    return '<div class="cards">%s</div>' % "".join(cards)


def _change_panel(snapshot, config):
    changes = snapshot.get("changes") or {}
    counts = monitor.change_counts(changes)
    summary = snapshot.get("summary") or ""
    narrative = (snapshot.get("report") or {}).get("narrative") or {}
    labels = (
        ("Catalog added", counts["catalog_added"]),
        ("Catalog removed", counts["catalog_removed"]),
        ("AA quality changes", counts["quality_changed"]),
        ("Rate changes", counts["rate_changed"]),
        ("Observed billing-rate drift", counts["observed_blend_changed"]),
    )
    if changes.get("first_run"):
        labels = labels + (("First recorded snapshot", "yes"),)
    items = "".join(
        '<span class="badge">%s: %s</span>' % (escape(label), escape(value))
        for label, value in labels
    )

    detail = []
    for model_id, entry in sorted((changes.get("rate_changed") or {}).items()):
        for rate_class, change in sorted(entry["classes"].items()):
            note = ''
            if change.get("note"):
                note = ' <span class="muted small">(%s)</span>' % escape(change["note"])
            detail.append(
                "<li>%s %s rate: %s to %s USD per 1M%s</li>"
                % (
                    escape(entry.get("name") or model_id),
                    escape(rate_class.replace("_", "-")),
                    fmt_usd_mtok(change["from"]),
                    fmt_usd_mtok(change["to"]),
                    note,
                )
            )
    for model_id, entry in sorted((changes.get("quality_changed") or {}).items()):
        detail.append(
            "<li>%s AA quality: %s to %s</li>"
            % (escape(entry.get("name") or model_id), fmt_points(entry.get("from")), fmt_points(entry.get("to")))
        )
    for model_id, entry in sorted((changes.get("observed_blend_changed") or {}).items()):
        detail.append(
            "<li>%s observed blended billing rate: %s to %s USD per 1M (%+.1f%%)</li>"
            % (
                escape(entry.get("name") or model_id),
                fmt_usd_mtok(entry.get("from")),
                fmt_usd_mtok(entry.get("to")),
                entry["change_ratio"] * 100.0,
            )
        )

    return (
        '<div class="panel"><h3>Weekly change summary</h3>'
        '<p class="sub">Produced by %s (%s). It reports only the detected changes and available '
        "model evidence.</p><p>%s</p>%s%s</div>"
    ) % (
        escape(narrative.get("model") or "the deterministic fallback"),
        escape(narrative.get("source") or "deterministic"),
        escape(summary),
        '<div class="badges">%s</div>' % items,
        ("<ul>%s</ul>" % "".join(detail)) if detail else "",
    )


def render_dashboard(snapshot, config, archive_entries=None):
    """The current dashboard: counts, best values, chart, table, and links."""
    report_meta = snapshot.get("report") or {}
    title = report_meta.get("title") or (config.get("report") or {}).get("title") or "ClinePass Models Monitoring"
    body = [
        "<h1>%s</h1>" % escape(title),
        '<p class="sub">Report generated %s (%s). Prices are Cline billing rates measured from this '
        "account's ClinePass usage; quality is the Artificial Analysis Intelligence Index.</p>"
        % (escape(report_meta.get("generated_at")), escape(report_meta.get("timezone"))),
        _badges(snapshot, config),
    ]
    warnings = report_meta.get("warnings") or []
    if warnings:
        body.append(
            '<div class="panel warnings"><h3>Run warnings</h3><ul>%s</ul></div>'
            % "".join("<li>%s</li>" % escape(warning) for warning in warnings)
        )
    body.append(_stat_cards(snapshot, config))
    body.append(_change_panel(snapshot, config))
    body.append("<h2>Cost and quality</h2>")
    body.append('<div class="chart">%s</div>' % render_chart(snapshot, config))
    body.append("<h2>Models</h2>")
    body.append(
        '<p class="sub">Click a header to sort. Per-column filters accept free text or numeric '
        "comparisons such as <span class=\"code\">&gt;=1</span>, <span class=\"code\">&lt;0.5</span> "
        "or <span class=\"code\">0.1</span>. Every rate cell carries its provenance: measured (in this "
        "account's billing window), reference (Cline's published rate card), free (catalog-declared "
        "$0) or input fallback.</p>"
    )
    body.append(render_table(snapshot, config))
    body.append("<h2>Methodology and history</h2>")
    body.append(
        '<p>Full methodology, provenance rules, and verification notes: '
        '<a href="methodology.html">methodology.html</a>. Quality source: '
        '<a href="https://artificialanalysis.ai/">Artificial Analysis</a>.</p>'
    )
    entries = archive_entries or []
    if entries:
        links = "".join(
            '<li><a href="archive/%s">%s report</a> <span class="muted small">(%s)</span></li>'
            % (escape(entry.get("report")), escape(entry.get("date")), escape(entry.get("mode")))
            for entry in entries
        )
        body.append(
            '<ul class="links">%s<li><a href="archive/index.html">All archived reports</a></li></ul>'
            % links
        )
    footer = (
        "<footer>Built by <span class=\"code\">python -m src.run</span> with no third-party "
        "dependencies. Mode: %s. Rate window: %s to %s. Fallback rate source: %s.</footer>"
        % (
            escape(report_meta.get("mode")),
            escape((report_meta.get("rate_window") or {}).get("start")),
            escape((report_meta.get("rate_window") or {}).get("end")),
            escape((snapshot.get("reference_rates") or {}).get("source_name")),
        )
    )
    return page(title, "\n".join(body), script=TABLE_SCRIPT, footer=footer)


def render_methodology(snapshot, config):
    """The methodology guide that is generated alongside every report."""
    report_meta = snapshot.get("report") or {}
    fit_config = config.get("rate_fit") or {}
    window = report_meta.get("rate_window") or {}
    reference = snapshot.get("reference_rates") or {}
    stats = snapshot.get("stats") or {}
    observation = snapshot.get("observation") or {}

    sections = [
        "<h1>Methodology</h1>",
        '<p class="sub">Generated %s (%s) in %s mode. This page describes exactly how the numbers on '
        "the dashboard are produced and where each rate came from.</p>"
        % (
            escape(report_meta.get("generated_at")),
            escape(report_meta.get("timezone")),
            escape(report_meta.get("mode")),
        ),
        "<h2>1. Quality</h2>",
        '<p>The AA Intelligence Index (Artificial Analysis) is the only quality input. It is '
        'normalized onto 0-100 with <span class="code">Q = clamp(100 * (s - min) / (max - min), '
        "0, 100)</span>, and a model without a current matched index is published with rates and no "
        "score rather than with an estimate. The Coding Index is display-only: it never affects "
        "cost, efficiency, or selection, and there is no minimum-quality gate. Source: "
        '<a href="https://artificialanalysis.ai/">artificialanalysis.ai</a>.</p>',
        "<h2>2. Prices</h2>",
        "<p>Rates are US dollars per one million tokens. Every rate field on the dashboard carries its "
        "provenance, and the three rate sources are described below.</p>",
    ]
    sections.extend(_methodology_rates(window, reference, fit_config))
    sections.extend(_methodology_scoring(config))
    sections.extend(_methodology_tail(snapshot, config, stats, observation))
    return page("Methodology", "\n".join(sections))


def _methodology_rates(window, reference, fit_config):
    """Section 2: the measured fit, the fallback table, and free models."""
    return [
        "<h3>Measured rates (primary)</h3>",
        "<p>For each ClinePass model the per-class rates are recovered from this account's own billing "
        "rows in the last %s days (%s to %s). ClinePass bills one reference cost per request, so "
        "input, output, and cache read are fitted jointly by non-negative least squares on</p>"
        '<p><span class="code">costUsd / 10^8 = (fresh_input * P_in + completion * P_out + cached * '
        'P_cache) / 10^6</span></p>'
        '<p>where <span class="code">fresh_input = promptTokens - cachedTokens</span>. A fit is used '
        "only when all of these hold: at least %s usable rows; the token-mix matrix has rank 3 with a "
        "condition number at most %s; the fitted input and output rates are strictly positive; the "
        "model total reconciles within %s; and the per-row RMS residual is within max(%s of the mean "
        'row cost, %s USD) to absorb integer rounding of <span class="code">costUsd</span>. A model '
        "whose fit is rejected falls back to the reference table, and the report says so.</p>"
        % (
            escape(window.get("days")),
            escape(window.get("start")),
            escape(window.get("end")),
            escape(fit_config.get("min_rows")),
            escape(fit_config.get("max_condition_number")),
            escape(fit_config.get("max_total_reconciliation_error")),
            escape(fit_config.get("rms_residual_ratio")),
            escape(fit_config.get("rms_residual_floor_usd")),
        ),
        "<h3>Reference rates (fallback)</h3>",
        '<p>%s, retrieved %s from <a href="%s">%s</a>, fills in models whose fit cannot be estimated '
        "and the cache-write class, which usage rows do not report separately. Measured and reference "
        "classes are never mixed inside one model, and a fallback value is never described as a "
        "measured current rate.</p>"
        % (
            escape(reference.get("source_name")),
            escape(reference.get("retrieved_at")),
            escape(reference.get("source_url")),
            escape(reference.get("source_url")),
        ),
        "<h3>Free models</h3>",
        '<p>Models in the catalog\'s free list carry declared zero rates with provenance '
        '<span class="code">catalog-free</span>. Free usage is documented as unavailable through the '
        "Cline API, so no billing rows are expected and the zero price cannot be confirmed from "
        "billing; it is a rotating promotion that can disappear.</p>",
    ]


def _methodology_scoring(config):
    """Section 3 and 4: task cost, profiles, efficiency, and the chart."""
    profiles = config.get("profiles") or {}
    profile_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (
            escape(profile.get("label") or name),
            fmt_int(profile.get("cache_read_tokens")),
            fmt_int(profile.get("fresh_input_tokens")),
            fmt_int(profile.get("cache_write_tokens")),
            fmt_int(profile.get("output_tokens")),
            escape(profile.get("penalty")),
        )
        for name, profile in profiles.items()
    )
    return [
        "<h2>3. Task cost and efficiency</h2>",
        '<p><span class="code">C = (T_cache * P_cache + T_in * P_in + T_write * P_write + T_out * '
        'P_out) / 10^6 * V</span>, where V is the per-model verbosity multiplier from '
        '<span class="code">scoring_config.json</span>. A missing cache-read or cache-write rate uses '
        "the input rate; an explicitly reported zero cache rate means free cache tokens. Missing or "
        "zero base input/output rates make the cost unavailable, except for a catalog-free model where "
        "zero is a declared promotion rather than missing data.</p>",
        '<table><thead><tr><th>Profile</th><th class="num">Cache read</th>'
        '<th class="num">Fresh input</th><th class="num">Cache write</th>'
        '<th class="num">Output</th><th>Penalty</th></tr></thead><tbody>%s</tbody></table>'
        % profile_rows,
        '<p>Efficiency is <span class="code">E = Q - penalty * log2(cost / median)</span> per profile, '
        "with the median taken over every discovered model that has a positive cost for that profile; "
        "quality never contributes to the median. Efficiency points are not dollars or credits. "
        "Zero-cost (free) models get no efficiency value, are excluded from both medians, and are "
        "marked free in the table.</p>",
        "<h2>4. Pareto, chart, and free models</h2>",
        "<p>Per profile, a model is Pareto-optimal when no other model has equal-or-higher quality and "
        "equal-or-lower cost with at least one strict improvement. Best-value cards pick the highest "
        "efficiency among that profile's Pareto models; there is no quality threshold, so a best-value "
        "card always names the best paid model, while free models dominate on price.</p>"
        "<p>The chart shows one point per eligible model, using the mean of its available profile "
        "costs on a logarithmic axis against the Intelligence Index, with a single non-dominated "
        "frontier and an annotation for every frontier model. A zero cost has no place on a logarithmic "
        "axis, so free models are drawn in the band on the left and stay in the non-dominated set.</p>",
    ]


def _methodology_tail(snapshot, config, stats, observation):
    """Sections 5 to 7: change detection scope, this run, and verification."""
    report_meta = snapshot.get("report") or {}
    change_config = config.get("change_detection") or {}
    narrative = config.get("narrative") or {}
    return [
        "<h2>5. Change detection</h2>",
        "<p>The delta compares catalog additions and removals, AA quality changes, per-class rate "
        "changes above %s, and observed blended-rate drift above %s. It does not compare every rate "
        "field, efficiency rank, or Pareto or recommendation shift. The summary is capped at %s words "
        "and must not invent missing values; if no narrative model can be used, a deterministic summary "
        "is published instead, and the dashboard names whichever produced it.</p>"
        % (
            escape(change_config.get("material_rate_change_ratio")),
            escape(change_config.get("observed_blend_change_ratio")),
            escape(narrative.get("max_words")),
        ),
        "<h2>6. This run</h2>",
        '<p>Catalog discovery: <span class="code">%s</span>. Usage rows normalised: %s, dropped as '
        "inconsistent: %s, inside the rate window: %s. Models discovered: %s, priced: %s, benchmarked: "
        "%s, with measured rates: %s, free: %s.</p>"
        % (
            escape((report_meta.get("catalog") or {}).get("source")),
            escape(observation.get("rows_total")),
            escape(observation.get("rows_dropped")),
            escape(observation.get("rows_in_window")),
            escape(stats.get("discovered")),
            escape(stats.get("priced")),
            escape(stats.get("benchmarked")),
            escape(stats.get("measured_rates")),
            escape(stats.get("free_models")),
        ),
        "<h2>7. Verification</h2>",
        "<p>The offline pipeline and its tests establish parser, rate-fitting, scoring, rendering, "
        "archive, and fixture behaviour only. An offline run uses fixtures, never touches Cline, "
        "Artificial Analysis, or OpenRouter, and must never be presented as a live evaluation.</p>"
        '<pre class="code">python -m pip install -r requirements.txt\n'
        "python -m unittest discover -s tests -v\n"
        "python -m src.run --offline</pre>",
    ]


def render_archive_index(snapshot, config, archive_entries):
    """The archive index listing every retained dated report."""
    entries = archive_entries or []
    if not entries:
        return page(
            "Archived reports",
            "<h1>Archived reports</h1><p>No dated report has been retained yet. "
            '<a href="../index.html">Back to the current report</a>.</p>',
        )
    rows = "".join(
        '<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td><a href="%s">report</a></td>'
        '<td><a href="%s">snapshot</a></td></tr>'
        % (
            escape(entry.get("date")),
            escape(entry.get("generated_at")),
            escape(entry.get("mode")),
            escape(entry.get("narrative_model")),
            escape(entry.get("report")),
            escape(entry.get("snapshot")),
        )
        for entry in entries
    )
    body = (
        "<h1>Archived reports</h1>"
        '<p class="sub">One dated report per run, newest first. '
        '<a href="../index.html">Back to the current report</a>.</p>'
        "<table><thead><tr><th>Date</th><th>Generated</th><th>Mode</th><th>Summary model</th>"
        "<th>Report</th><th>Snapshot</th></tr></thead><tbody>%s</tbody></table>"
    ) % rows
    return page("Archived reports", body)


# ---------------------------------------------------------------------------
# History and site output
# ---------------------------------------------------------------------------


def snapshot_filename(day, mode):
    """Offline snapshots get their own suffix so they never block a live run."""
    if mode == monitor.MODE_OFFLINE:
        return "%s%s" % (day, SNAPSHOT_SUFFIX_OFFLINE)
    return "%s.json" % day


def write_snapshot(history_dir, snapshot, mode):
    """Write the dated snapshot and return its path."""
    day = (snapshot.get("report") or {}).get("date")
    if not day:
        raise ValueError("snapshot has no report date")
    path = Path(history_dir) / snapshot_filename(day, mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    return path


def read_snapshot(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def history_snapshots(history_dir, mode):
    """Dated snapshots for one mode, newest first."""
    directory = Path(history_dir)
    if not directory.exists():
        return []
    paths = []
    for path in directory.glob("*.json"):
        if path.name == ARCHIVE_FILE:
            continue
        is_offline = path.name.endswith(SNAPSHOT_SUFFIX_OFFLINE)
        if is_offline != (mode == monitor.MODE_OFFLINE):
            continue
        paths.append(path)
    return sorted(paths, key=lambda item: item.name, reverse=True)


def find_previous_snapshot(history_dir, before_date, mode, exclude=None):
    """The latest snapshot strictly earlier than ``before_date``."""
    for path in history_snapshots(history_dir, mode):
        day = path.name.split(".")[0]
        if exclude is not None and path.name == Path(exclude).name:
            continue
        if day < before_date:
            return read_snapshot(path), path
    return None, None


def snapshot_exists(history_dir, day, mode):
    """Whether a snapshot for this date and mode has already been written."""
    return (Path(history_dir) / snapshot_filename(day, mode)).exists()


def read_archive(archive_path):
    """Existing archive entries, or an empty list when the file is unusable."""
    path = Path(archive_path)
    if not path.exists():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
        return [item for item in loaded["entries"] if isinstance(item, dict)]
    return []


def update_archive(archive_path, entry, limit):
    """Insert one archive entry, newest first, keeping the newest ``limit`` entries."""
    path = Path(archive_path)
    entries = [item for item in read_archive(path) if item.get("date") != entry.get("date")]
    entries.append(entry)
    entries.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
    entries = entries[: max(1, int(limit))]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "entries": entries}, indent=2) + "\n", encoding="utf-8"
    )
    return entries


def write_site(site_dir, snapshot, config, archive_entries):
    """Write the dashboard, methodology page, dated report, archive index, and snapshot copy."""
    site = Path(site_dir)
    archive_dir = site / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    day = (snapshot.get("report") or {}).get("date") or "undated"
    written = []

    (site / "index.html").write_text(
        render_dashboard(snapshot, config, archive_entries), encoding="utf-8"
    )
    written.append(site / "index.html")

    dated = archive_dir / ("%s.html" % day)
    dated.write_text(render_dashboard(snapshot, config, []), encoding="utf-8")
    written.append(dated)

    methodology = site / "methodology.html"
    methodology.write_text(render_methodology(snapshot, config), encoding="utf-8")
    written.append(methodology)

    index = archive_dir / "index.html"
    index.write_text(
        render_archive_index(snapshot, config, archive_entries), encoding="utf-8"
    )
    written.append(index)

    snapshot_copy = site / "snapshot.json"
    snapshot_copy.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    written.append(snapshot_copy)
    return written