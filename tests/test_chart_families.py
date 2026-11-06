"""Chart colours by model family and non-overlapping point labels.

The chart used to colour every dot by role (paid / frontier / best) and label
only the frontier members, so a reader could not tell a cheap DeepSeek from a
cheap Qwen and most points had no name at all. Colour now encodes the family,
the role moved to the ring, and every marker is labelled through a placement
pass that refuses to overlap the other labels, the markers, the tick labels, or
the legend box.
"""

import itertools
import re
import unittest

from src import monitor, site


def report(points):
    """A minimal report with one chart point per (id, family, quality, cost)."""
    return {
        "chart": {
            "points": [
                {
                    "id": model_id,
                    "name": name,
                    "family": family,
                    "quality": quality,
                    "cost": cost,
                    "free": False,
                    "profiles": ["planning"],
                    "frontier": index == 0,
                }
                for index, (model_id, name, family, quality, cost) in enumerate(points)
            ],
            "free_band": [],
        },
        "best_value": {},
    }


def labels(svg):
    return re.findall(
        r'<text data-role="annotation" data-model-id="([^"]*)" class="chart-label" '
        r'x="([\d.]+)" y="([\d.]+)" text-anchor="(\w+)">(.*?)</text>',
        svg,
    )


def label_rects(svg):
    return [
        site._label_rect(text, float(x), float(y), anchor)
        for _id, x, y, anchor, text in labels(svg)
    ]


class FamilyColorTests(unittest.TestCase):
    def test_each_family_gets_its_own_colour(self):
        families = ["deepseek", "glm", "kimi", "mimo", "qwen"]
        colours = [site.family_color(family) for family in families]
        self.assertEqual(len(families), len(set(colours)))
        for colour in colours:
            self.assertRegex(colour, r"^#[0-9a-f]{6}$")

    def test_the_colour_is_stable_across_calls(self):
        self.assertEqual(site.family_color("deepseek"), site.family_color("deepseek"))
        self.assertEqual(site.family_color("mimo"), site.family_color("mimo"))

    def test_an_unknown_family_still_gets_a_palette_colour(self):
        colour = site.family_color("some-new-lab")
        self.assertIn(colour, site.FAMILY_PALETTE)

    def test_the_dots_are_filled_with_their_family_colour(self):
        data = [
            ("a", "A", "deepseek", 50.0, 0.01),
            ("b", "B", "glm", 40.0, 0.02),
            ("c", "C", "qwen", 60.0, 0.05),
        ]
        svg = site.render_chart(report(data), None)
        fills = re.findall(r'r="5" style="fill:(#[0-9a-f]+)"', svg)
        self.assertEqual(
            [site.family_color(family) for _, _, family, _, _ in data], fills
        )

    def test_the_palette_avoids_the_role_ring_colours(self):
        # Frontier green and best-value gold stay reserved for the rings.
        self.assertNotIn("#1a7f37", site.FAMILY_PALETTE)
        self.assertNotIn("#9a6700", site.FAMILY_PALETTE)


class LabelPlacementTests(unittest.TestCase):
    def test_every_marker_gets_exactly_one_label(self):
        data = [
            ("a", "Alpha One", "deepseek", 50.0, 0.01),
            ("b", "Beta Two", "glm", 40.0, 0.02),
            ("c", "Gamma Three", "qwen", 60.0, 0.05),
        ]
        svg = site.render_chart(report(data), None)
        names = [label[0] for label in labels(svg)]
        self.assertEqual(sorted(point[0] for point in data), sorted(names))

    def test_no_two_labels_overlap(self):
        data = [
            ("a", "DeepSeek V4.1 Flash", "deepseek", 39.5, 0.0153),
            ("b", "GLM 5.3", "glm", 44.8, 0.2185),
            ("c", "Kimi K3", "kimi", 43.6, 0.4744),
            ("d", "MiniMax M3", "minimax", 29.2, 0.0468),
            ("e", "Qwen3.7 Plus", "qwen", 25.2, 0.0827),
            ("f", "Qwen3.7 Max", "qwen", 29.5, 0.3706),
            ("g", "MiMo-V2.6-Flash", "mimo", 37.9, 0.0111),
            ("h", "Muse Spark 1.3", "muse", 48.1, 0.0079),
        ]
        svg = site.render_chart(report(data), None)
        rects = label_rects(svg)
        self.assertEqual(len(data), len(rects))
        for first, second in itertools.combinations(rects, 2):
            self.assertFalse(
                site._rects_collide(first, second),
                "labels overlap: %r and %r" % (first, second),
            )

    def test_a_crowded_chart_still_labels_every_point_without_overlap(self):
        # Twelve models packed into the same corner of the plot: the naive
        # "always to the right, stacked by index" approach collides here.
        data = [
            ("m%d" % index, "Model %d" % index, "deepseek", 40.0 + (index % 3), 0.02)
            for index in range(12)
        ]
        svg = site.render_chart(report(data), None)
        rects = label_rects(svg)
        self.assertEqual(len(data), len(rects))
        for first, second in itertools.combinations(rects, 2):
            self.assertFalse(site._rects_collide(first, second))

    def test_labels_stay_inside_the_plot_and_above_the_x_ticks(self):
        data = [
            ("a", "Edge Cheap", "deepseek", 10.0, 0.001),
            ("b", "Edge Pricey", "glm", 95.0, 50.0),
            ("c", "Middle", "qwen", 50.0, 0.5),
        ]
        svg = site.render_chart(report(data), None)
        for _id, x, y, anchor, text in labels(svg):
            rect = site._label_rect(text, float(x), float(y), anchor)
            self.assertGreaterEqual(rect[0], site.CHART_LEFT)
            self.assertLessEqual(rect[2], site.CHART_RIGHT)
            self.assertGreaterEqual(rect[1], site.CHART_TOP)
            self.assertLessEqual(rect[3], site.CHART_BOTTOM)
            self.assertLess(float(y), site.CHART_BOTTOM + 18)

    def test_a_displaced_label_gets_a_leader_line(self):
        data = [
            ("m%d" % index, "Model %d" % index, "qwen", 40.0 + (index % 2) * 0.2, 0.02)
            for index in range(6)
        ]
        svg = site.render_chart(report(data), None)
        self.assertIn('class="leader"', svg)
        for match in re.finditer(
            r'<line class="leader" x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"', svg
        ):
            for value in match.groups():
                self.assertGreaterEqual(float(value), 0.0)

    def test_a_missing_family_does_not_break_the_chart(self):
        svg = site.render_chart(
            report([("a", "No Family", "", 50.0, 0.01)]), None
        )
        self.assertEqual(1, len(labels(svg)))


class LabelTextTests(unittest.TestCase):
    def test_a_vendor_prefix_is_dropped(self):
        self.assertEqual("DeepSeek V4.1 Flash 1017", site.label_text("DeepSeek: DeepSeek V4.1 Flash 1017"))
        self.assertEqual("Nemotron 3.5 Super VL", site.label_text("NVIDIA: Nemotron 3.5 Super VL"))

    def test_a_short_name_is_kept_whole(self):
        self.assertEqual("GLM 5.3", site.label_text("Z.ai: GLM 5.3"))
        self.assertEqual("Solar Mini 4", site.label_text("Upstage: Solar Mini 4"))

    def test_a_long_name_is_cut_at_a_word_boundary(self):
        text = site.label_text("NVIDIA: Nemotron 3.5 Lightning 30B-A3B")
        self.assertLessEqual(len(text), site.LABEL_MAX_CHARS)
        self.assertTrue(text.endswith("..."))
        self.assertNotIn("  ", text)
        self.assertNotIn("Lightni...", text)

    def test_no_label_exceeds_the_documented_width(self):
        for name in ("A" * 80, "Word " * 20, "DeepSeek: " + "X" * 40):
            self.assertLessEqual(len(site.label_text(name)), site.LABEL_MAX_CHARS)


class ChartLegendTests(unittest.TestCase):
    def test_the_family_legend_lists_exactly_the_families_in_the_chart(self):
        data = [
            ("a", "A", "deepseek", 50.0, 0.01),
            ("b", "B", "glm", 40.0, 0.02),
            ("c", "C", "deepseek", 45.0, 0.03),
        ]
        snapshot = report(data)
        families = site.chart_families(snapshot)
        self.assertEqual(["deepseek", "glm"], [name for name, _ in families])
        html = site._chart_legend_list(snapshot)
        self.assertIn("deepseek", html)
        self.assertIn("glm", html)
        self.assertNotIn("qwen", html)
        self.assertIn(site.family_color("deepseek"), html)

    def test_the_in_chart_legend_still_explains_the_roles(self):
        svg = site.render_chart(report([("a", "A", "glm", 50.0, 0.01)]), None)
        self.assertIn("Pareto / efficient frontier", svg)
        self.assertIn("colour = family", svg)
        self.assertIn("best value", svg)


class NotPlottedNoteTests(unittest.TestCase):
    """A model missing from the chart must say why, not just disappear.

    The chart plots a model only when it has both a current AA score and a
    usable cost, so a ClinePass model missing from it used to look like a model
    the monitor never found.
    """

    def snapshot(self):
        snapshot = report([("a", "A", "glm", 50.0, 0.01)])
        snapshot["models"] = [
            {
                "id": "a",
                "name": "A",
                "quality": {"intelligence_index": 50.0},
                "cost_usd": {"execution": 0.01},
                "quality_evidence": {},
            },
            {
                "id": "b",
                "name": "Kimi K3",
                "quality": {},
                "cost_usd": {"execution": 0.6},
                "quality_evidence": {
                    "reason": "pre-revision",
                    "last_value": 43.6,
                    "last_seen": "2026-10-26",
                    "revision_day": "2026-10-28",
                },
            },
            {
                "id": "c",
                "name": "Qwen3.8 Max",
                "quality": {},
                "cost_usd": {"execution": 0.28},
                "quality_evidence": {"reason": "absent-from-feed"},
            },
            {
                "id": "d",
                "name": "MiniMax M3.1 Flash",
                "quality": {"intelligence_index": 29.1},
                "cost_usd": {"execution": None},
                "quality_evidence": {},
            },
        ]
        return snapshot

    def test_every_excluded_model_is_named_with_its_reason(self):
        svg = site.render_chart(self.snapshot(), None)
        self.assertIn('data-role="chart-excluded"', svg)
        self.assertIn("Kimi K3 (no current AA score (last 43.60 on 2026-10-26", svg)
        self.assertIn("Qwen3.8 Max (no current AA score)", svg)
        self.assertIn("MiniMax M3.1 Flash (no usable cost)", svg)
        self.assertNotIn("Not plotted: A", svg)

    def test_a_fully_plotted_report_has_no_note(self):
        snapshot = report([("a", "A", "glm", 50.0, 0.01)])
        snapshot["models"] = [
            {
                "id": "a",
                "name": "A",
                "quality": {"intelligence_index": 50.0},
                "cost_usd": {"execution": 0.01},
                "quality_evidence": {},
            }
        ]
        self.assertNotIn("chart-excluded", site.render_chart(snapshot, None))


if __name__ == "__main__":
    unittest.main()
