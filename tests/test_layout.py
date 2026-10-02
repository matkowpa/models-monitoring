"""Layout regression checks for the generated dashboard and chart.

These assert the structural properties that actually break a page: ragged table
rows, a table with no horizontal overflow container, the two sticky header rows
overlapping, an axis title inside the free band, annotations escaping the plot,
and text drawn outside the SVG viewBox.
"""

import re
import unittest
from html.parser import HTMLParser

from tests.support import FixtureTestCase
from src import site


class _RowCounter(HTMLParser):
    """Collects the cell counts per table row, including filter inputs."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._current = []
        if tag in ("th", "td") and self._current is not None:
            self._current.append(tag)

    def handle_endtag(self, tag):
        if tag == "tr" and self._current is not None:
            self.rows.append(self._current)
            self._current = None


class DashboardLayoutTests(FixtureTestCase):
    def setUp(self):
        super().setUp()
        self.snapshot = self.run_offline()
        self.html = (self.site_dir / "index.html").read_text(encoding="utf-8")
        self.css = site.CSS

    def test_the_table_has_no_ragged_rows(self):
        counter = _RowCounter()
        counter.feed(self.html)
        self.assertGreater(len(counter.rows), 2)
        widths = {len(row) for row in counter.rows}
        self.assertEqual(1, len(widths), "rows disagree about their cell count: %s" % widths)
        self.assertEqual(13, widths.pop())

    def test_the_table_is_wrapped_in_a_horizontal_overflow_container(self):
        self.assertIn('<div class="table-wrap"><table id="models">', self.html)
        self.assertIn("</tbody></table></div>", self.html)
        self.assertIn("overflow: auto", self.css)
        self.assertIn("min-width: 1180px", self.css)

    def test_the_table_wrapper_is_its_own_scrollport_so_sticky_rows_can_stick(self):
        wrapper = re.search(r"\.table-wrap \{([^}]*)\}", self.css).group(1)
        self.assertIn("overflow: auto", wrapper)
        self.assertRegex(wrapper, r"max-height: \d+vh")
        self.assertIn("border-radius", wrapper)

    def test_the_single_sticky_header_row_sticks_to_the_top(self):
        header = re.search(r"#models thead th\[data-key\] \{([^}]*)\}", self.css).group(1)
        self.assertIn("position: sticky", header)
        self.assertIn("top: 0", header)
        self.assertNotIn("tr.filters", self.css)
        self.assertNotIn("data-filter=", self.html)

    def test_long_values_cannot_stretch_the_layout(self):
        self.assertIn("overflow-wrap: anywhere", self.css)
        card_value = re.search(r"\.card \.v \{([^}]*)\}", self.css).group(1)
        self.assertIn("overflow-wrap: anywhere", card_value)

    def test_filtered_out_rows_stay_hidden(self):
        self.assertIn("#models tbody tr[hidden] { display: none; }", self.css)

    def test_the_chart_keeps_a_usable_width_when_the_viewport_is_narrow(self):
        self.assertIn("min-width: 700px", self.css)
        self.assertIn("overflow-x: auto", self.css)

    def test_every_row_carries_the_attributes_the_script_filters_on(self):
        keys = (
            "model",
            "family",
            "source",
            "quality",
            "input",
            "costplanning",
            "costexecution",
            "effplanning",
            "effexecution",
            "benchmarked",
            "priced",
            "free",
            "measured",
            "frontier",
            "best",
        )
        body = self.html[self.html.index("<tbody>") :]
        rows = re.findall(r"<tr ([^>]*)>", body)
        self.assertEqual(self.snapshot["stats"]["discovered"], len(rows))
        for row in rows:
            for key in keys:
                self.assertIn("data-%s=" % key, row)

    def test_the_table_has_no_per_column_filter_row(self):
        self.assertNotIn('<tr class="filters">', self.html)
        self.assertNotIn("data-filter=", self.html)
        self.assertIn('id="model-search"', self.html)
        self.assertIn('id="family-filter"', self.html)


class ChartGeometryTests(FixtureTestCase):
    def setUp(self):
        super().setUp()
        self.snapshot = self.run_offline()
        self.svg = site.render_chart(self.snapshot, self.config)
        view = re.search(r'viewBox="0 0 (\d+) (\d+)"', self.svg)
        self.width = int(view.group(1))
        self.height = int(view.group(2))

    def coordinates(self):
        xs, ys = [], []
        for match in re.finditer(r'<text[^>]*\bx="(-?[\d.]+)"[^>]*\by="(-?[\d.]+)"', self.svg):
            xs.append(float(match.group(1)))
            ys.append(float(match.group(2)))
        for match in re.finditer(r'<circle[^>]*\bcx="(-?[\d.]+)"[^>]*\bcy="(-?[\d.]+)"', self.svg):
            xs.append(float(match.group(1)))
            ys.append(float(match.group(2)))
        return xs, ys

    def test_nothing_is_drawn_outside_the_viewbox(self):
        xs, ys = self.coordinates()
        self.assertGreaterEqual(min(xs), 0)
        self.assertLessEqual(max(xs), self.width)
        self.assertGreaterEqual(min(ys), 0)
        self.assertLessEqual(max(ys), self.height)

    def test_the_axis_title_sits_left_of_the_free_band(self):
        title = re.search(r"transform=\"translate\(([\d.]+),[\d.]+\) rotate\(-90\)\"", self.svg)
        self.assertIsNotNone(title, "the rotated y-axis title is missing")
        self.assertLess(float(title.group(1)), site.FREE_BAND_LEFT)

    def test_y_tick_labels_clear_the_free_band(self):
        labels = [
            float(match.group(1))
            for match in re.finditer(r'<text x="([\d.]+)" y="[\d.]+" text-anchor="end">', self.svg)
        ]
        self.assertTrue(labels)
        for x in labels:
            self.assertGreaterEqual(x, site.FREE_BAND_RIGHT)

    def test_the_free_band_label_is_above_the_plot_and_markers_sit_inside_it(self):
        self.assertTrue(self.snapshot["chart"]["free_band"])
        label = re.search(
            r'<text x="([\d.]+)" y="([\d.]+)" text-anchor="middle" data-role="free-band-label">',
            self.svg,
        )
        self.assertIsNotNone(label)
        self.assertLess(float(label.group(2)), site.CHART_TOP)
        markers = re.findall(
            r'<g data-role="point" data-model-id="[^"]*" data-frontier="[^"]*" data-best="[^"]*" '
            r'data-free="true">'
            r'<circle class="point point-free[^"]*" cx="([\d.]+)"',
            self.svg,
        )
        self.assertEqual(len(self.snapshot["chart"]["free_band"]), len(markers))
        for x in markers:
            self.assertGreaterEqual(float(x), site.FREE_BAND_LEFT)
            self.assertLessEqual(float(x), site.FREE_BAND_RIGHT)

    def test_the_frontier_is_a_stepped_pareto_curve(self):
        self.assertIn('data-role="frontier"', self.svg)
        path = re.search(r'<path class="frontier" data-role="frontier" d="([^"]+)"', self.svg)
        self.assertIsNotNone(path, "the frontier renders as a stepped path")
        segments = re.findall(r"[ML] ([\d.]+),([\d.]+)", path.group(1))
        self.assertGreaterEqual(len(segments), 3, "the curve spans the plot with edge extensions")
        for x, y in segments:
            self.assertGreaterEqual(float(x), site.CHART_LEFT)
            self.assertLessEqual(float(x), site.CHART_RIGHT)
            self.assertGreaterEqual(float(y), site.CHART_TOP)
            self.assertLessEqual(float(y), site.CHART_BOTTOM)
        self.assertIn('data-role="frontier-area"', self.svg)
        # The curve always starts at the left edge and ends at the right edge,
        # so even a single-frontier-member week draws a visible line.
        self.assertLessEqual(float(segments[0][0]), site.CHART_LEFT)
        self.assertGreaterEqual(float(segments[-1][0]), site.CHART_RIGHT)

    def test_frontier_and_best_value_points_are_colored(self):
        self.assertIn("point-frontier", self.svg)
        self.assertIn("point-best", self.svg)
        self.assertIn("Pareto / efficient frontier", self.svg)
        best_ids = {
            entry.get("id")
            for entry in (self.snapshot.get("best_value") or {}).values()
            if isinstance(entry, dict) and entry.get("id")
        }
        self.assertTrue(best_ids)
        for model_id in best_ids:
            self.assertIn('data-best="true"', self.svg)

    def test_annotations_stay_inside_the_plot_area(self):
        ys = [
            float(match.group(1))
            for match in re.finditer(r'<text data-role="annotation"[^>]*y="([\d.]+)"', self.svg)
        ]
        self.assertTrue(ys)
        for y in ys:
            self.assertGreaterEqual(y, site.CHART_TOP)
            self.assertLessEqual(y, site.CHART_BOTTOM)

    def test_annotations_clear_the_x_tick_labels(self):
        tick_ys = [
            float(match.group(2))
            for match in re.finditer(
                r'<text x="(-?[\d.]+)" y="(-?[\d.]+)" text-anchor="middle">[-\d.]+</text>', self.svg
            )
        ]
        self.assertTrue(tick_ys, "expected x tick labels")
        for match in re.finditer(r'<text data-role="annotation"[^>]*y="([\d.]+)"', self.svg):
            self.assertLess(float(match.group(1)), min(tick_ys))


class LinkIntegrityTests(FixtureTestCase):
    """Every relative link in the published site must resolve to a real file."""

    def setUp(self):
        super().setUp()
        self.run_offline()

    def relative_links(self, page_path):
        text = page_path.read_text(encoding="utf-8")
        links = []
        for match in re.finditer(r'href="([^"]+)"', text):
            target = match.group(1)
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            links.append(target)
        return links

    def test_every_relative_link_resolves(self):
        pages = sorted(self.site_dir.rglob("*.html"))
        self.assertGreaterEqual(len(pages), 3)
        checked = 0
        for page_path in pages:
            for target in self.relative_links(page_path):
                resolved = (page_path.parent / target.split("#")[0]).resolve()
                self.assertTrue(
                    resolved.exists(),
                    "%s links to missing %s" % (page_path.name, target),
                )
                checked += 1
        self.assertGreater(checked, 0, "expected at least one relative link to check")

    def test_no_link_points_at_unpublished_snapshot_data(self):
        for page_path in sorted(self.site_dir.rglob("*.html")):
            for target in self.relative_links(page_path):
                self.assertNotIn("history", target)
                self.assertNotIn(".offline.json", target)

    def test_the_current_report_links_its_methodology_and_archive(self):
        links = self.relative_links(self.site_dir / "index.html")
        self.assertIn("methodology.html", links)
        self.assertIn("archive/index.html", links)


if __name__ == "__main__":
    unittest.main()