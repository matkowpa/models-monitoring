"""Rendering, interactions, archive, and history (section 7 steps 6 and 7)."""

import unittest

from tests.support import FixtureTestCase
from src import monitor, site


class OfflinePipelineTests(FixtureTestCase):
    def setUp(self):
        super().setUp()
        self.snapshot = self.run_offline()
        self.dashboard = (self.site_dir / "index.html").read_text(encoding="utf-8")
        self.methodology = (self.site_dir / "methodology.html").read_text(encoding="utf-8")
        self.date = self.snapshot["report"]["date"]

    def test_run_writes_every_documented_output(self):
        for name in ("index.html", "methodology.html", "snapshot.json", "archive.json"):
            self.assertTrue((self.site_dir / name).exists(), name)
        self.assertTrue((self.site_dir / "archive" / (self.date + ".html")).exists())
        self.assertTrue((self.site_dir / "archive" / "index.html").exists())

    def test_snapshot_records_complete_metadata(self):
        report = self.snapshot["report"]
        for key in (
            "generated_at",
            "date",
            "timezone",
            "mode",
            "catalog",
            "narrative",
            "rate_window",
            "money_scales",
            "warnings",
        ):
            self.assertIn(key, report)
        self.assertEqual(monitor.MODE_OFFLINE, report["mode"])
        self.assertEqual("Europe/Warsaw", report["timezone"])
        for key in (
            "account",
            "reference_rates",
            "observation",
            "stats",
            "medians",
            "best_value",
            "chart",
            "models",
            "changes",
            "summary",
        ):
            self.assertIn(key, self.snapshot)
        for model in self.snapshot["models"]:
            for rate_class in monitor.RATE_CLASSES:
                self.assertIn(rate_class, model["rates"])
                self.assertIn("provenance", model["rates"][rate_class])

    def test_offline_runs_are_labelled_and_never_claim_to_be_live(self):
        self.assertIn("OFFLINE", self.dashboard)
        self.assertNotIn('class="badge live"', self.dashboard)
        self.assertIn("not a live evaluation", self.dashboard)

    def test_dashboard_shows_the_report_timestamp_and_provenance(self):
        self.assertIn(self.snapshot["report"]["generated_at"], self.dashboard)
        for needle in ("measured", "reference", "free"):
            self.assertIn(needle, self.dashboard)

    def test_dashboard_lists_discovered_and_measured_counts(self):
        stats = self.snapshot["stats"]
        self.assertIn("Models discovered", self.dashboard)
        self.assertIn(str(stats["discovered"]), self.dashboard)
        self.assertGreater(stats["measured_rates"], 0)
        self.assertGreater(stats["free_models"], 0)

    def test_methodology_page_states_the_formulas_and_profiles(self):
        for needle in (
            "AA Intelligence Index",
            "non-negative least squares",
            "log2(cost / median)",
            "Planning",
            "Execution",
            "catalog-free",
            "ClinePass",
        ):
            self.assertIn(needle, self.methodology)


class ChartTests(FixtureTestCase):
    def setUp(self):
        super().setUp()
        self.snapshot = self.run_offline()
        self.svg = site.render_chart(self.snapshot, self.config)

    def test_each_eligible_model_is_plotted_once(self):
        plotted = [point["id"] for point in self.snapshot["chart"]["points"]]
        self.assertEqual(len(plotted), len(set(plotted)))
        for model_id in plotted:
            self.assertEqual(
                1, self.svg.count('data-role="point" data-model-id="%s"' % model_id), model_id
            )

    def test_the_frontier_is_non_dominated_and_every_member_is_annotated(self):
        frontier = [point for point in self.snapshot["chart"]["points"] if point["frontier"]]
        self.assertTrue(frontier)
        self.assertIn('data-role="frontier"', self.svg)
        for point in frontier:
            self.assertEqual(
                1,
                self.svg.count('data-role="annotation" data-model-id="%s"' % point["id"]),
                point["id"],
            )

    def test_no_plotted_frontier_member_is_in_fact_dominated(self):
        points = self.snapshot["chart"]["points"]
        members = monitor.pareto_members(points)
        for point in points:
            if point["frontier"]:
                self.assertTrue(members[point["id"]], point["id"])

    def test_free_models_are_in_the_left_band(self):
        band = self.snapshot["chart"]["free_band"]
        self.assertTrue(band)
        self.assertIn('data-role="free-band"', self.svg)
        self.assertIn("$0 (free)", self.svg)
        plotted = [point["id"] for point in self.snapshot["chart"]["points"]]
        for point in band:
            self.assertEqual(0.0, point["cost"])
            self.assertNotIn(point["id"], plotted)
            self.assertIn('data-model-id="%s"' % point["id"], self.svg)

    def test_the_legend_sits_inside_the_lower_right_of_the_plot(self):
        marker = self.svg.index('data-role="legend"')
        fragment = self.svg[marker : marker + 160]
        x = int(fragment.split('data-x="')[1].split('"')[0])
        y = int(fragment.split('data-y="')[1].split('"')[0])
        self.assertGreater(x, (site.CHART_LEFT + site.CHART_RIGHT) / 2)
        self.assertGreater(y, (site.CHART_TOP + site.CHART_BOTTOM) / 2)
        self.assertLess(x, site.CHART_RIGHT)
        self.assertLess(y, site.CHART_BOTTOM)

    def test_empty_chart_renders_an_explanation_instead_of_an_axis(self):
        empty = site.render_chart({"chart": {"points": [], "free_band": []}}, self.config)
        self.assertIn('data-role="chart-empty"', empty)
        self.assertNotIn('data-role="frontier"', empty)

    def test_a_single_eligible_model_still_plots_and_annotates(self):
        report = {
            "chart": {
                "points": [
                    {
                        "id": "x",
                        "name": "X",
                        "quality": 50.0,
                        "cost": 0.5,
                        "free": False,
                        "profiles": ["planning"],
                        "frontier": True,
                    }
                ],
                "free_band": [],
            },
            "best_value": {
                "planning": {"id": "x", "name": "X"},
            },
        }
        svg = site.render_chart(report, self.config)
        self.assertIn('data-model-id="x"', svg)
        self.assertIn('data-role="annotation" data-model-id="x"', svg)
        self.assertIn('data-role="frontier"', svg)
        self.assertIn("point-best", svg)


if __name__ == "__main__":
    unittest.main()