"""Sanity check: render the chart from the real snapshot with the fixed frontier flags."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import monitor, site

snapshot = json.load(open("data/history/2026-10-02.json", encoding="utf-8"))
chart = snapshot["chart"]
plotted, band = chart["points"], chart["free_band"]

paid = monitor.pareto_members(plotted)
combined = monitor.pareto_members(plotted + band)
for point in plotted:
    point["frontier"] = bool(paid.get(point["id"]))
for point in band:
    point["frontier"] = bool(combined.get(point["id"]))

svg = site.render_chart({"chart": chart, "best_value": snapshot["best_value"]})
curve = re.search(r'<path class="frontier" data-role="frontier" d="([^"]+)"', svg)
area = re.search(r'frontier-area" d="([^"]+)"', svg)
print("CURVE :", curve.group(1) if curve else "NOT FOUND")
print("AREA  :", (area.group(1)[:100] + "...") if area else "NOT FOUND")
print("FLAGS :", [(p["id"], round(p["cost"], 4), p["quality"]) for p in plotted + band if p["frontier"]])
sys.exit(0 if curve else 1)
