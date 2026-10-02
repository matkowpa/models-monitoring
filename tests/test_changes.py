"""Change detection and the deterministic summary (section 4)."""

import unittest

from tests.support import config
from src import monitor

CHANGE_CONFIG = config()["change_detection"]


def model(model_id, quality=None, rates=None, observed=None, name=None):
    """A minimal model record for snapshot comparison."""
    return {
        "id": model_id,
        "name": name or model_id,
        "quality": None if quality is None else {"intelligence_index": quality},
        "rates": {
            rate_class: {"usd_per_mtok": value, "provenance": provenance}
            for rate_class, (value, provenance) in (rates or {}).items()
        },
        "observed_blended_usd_per_mtok": observed,
    }


def snapshot(models):
    return {"models": models}


class FirstRunTests(unittest.TestCase):
    def test_a_first_snapshot_reports_no_deltas(self):
        current = snapshot([model("cline-pass/a", 70.0, {"input": (1.0, "measured")})])
        changes = monitor.detect_changes(None, current, CHANGE_CONFIG)
        self.assertTrue(changes["first_run"])
        self.assertFalse(changes["has_previous"])
        self.assertEqual([], changes["catalog_added"])
        self.assertEqual({}, changes["rate_changed"])
        self.assertIn("First recorded snapshot", monitor.deterministic_summary(changes))

    def test_an_empty_previous_snapshot_is_treated_as_a_first_run(self):
        changes = monitor.detect_changes(
            snapshot([]), snapshot([model("cline-pass/a")]), CHANGE_CONFIG
        )
        self.assertTrue(changes["first_run"])
        self.assertEqual([], changes["catalog_added"])


class CatalogDeltaTests(unittest.TestCase):
    def test_additions_and_removals_are_reported(self):
        previous = snapshot([model("cline-pass/a"), model("cline-pass/b")])
        current = snapshot([model("cline-pass/b"), model("cline-pass/c")])
        changes = monitor.detect_changes(previous, current, CHANGE_CONFIG)
        self.assertEqual(["cline-pass/c"], changes["catalog_added"])
        self.assertEqual(["cline-pass/a"], changes["catalog_removed"])
        counts = monitor.change_counts(changes)
        self.assertEqual(1, counts["catalog_added"])
        self.assertEqual(1, counts["catalog_removed"])


class QualityDeltaTests(unittest.TestCase):
    def test_quality_changes_are_reported_with_both_values(self):
        previous = snapshot([model("cline-pass/a", 70.0, name="Model A")])
        current = snapshot([model("cline-pass/a", 68.5, name="Model A")])
        changes = monitor.detect_changes(previous, current, CHANGE_CONFIG)
        self.assertEqual({"name": "Model A", "from": 70.0, "to": 68.5}, changes["quality_changed"]["cline-pass/a"])

    def test_a_newly_scored_or_cleared_score_is_a_change(self):
        unscored = snapshot([model("cline-pass/a", None)])
        scored = snapshot([model("cline-pass/a", 61.0)])
        self.assertIn(
            "cline-pass/a", monitor.detect_changes(unscored, scored, CHANGE_CONFIG)["quality_changed"]
        )
        cleared = monitor.detect_changes(scored, unscored, CHANGE_CONFIG)
        self.assertEqual({"name": "cline-pass/a", "from": 61.0, "to": None}, cleared["quality_changed"]["cline-pass/a"])

    def test_an_unchanged_score_is_not_a_change(self):
        changes = monitor.detect_changes(
            snapshot([model("cline-pass/a", 70.0)]),
            snapshot([model("cline-pass/a", 70.0)]),
            CHANGE_CONFIG,
        )
        self.assertEqual({}, changes["quality_changed"])


class RateDeltaTests(unittest.TestCase):
    def test_material_rate_changes_are_reported_per_class(self):
        previous = snapshot(
            [model("cline-pass/a", rates={"input": (1.0, "measured"), "output": (2.0, "measured")})]
        )
        current = snapshot(
            [model("cline-pass/a", rates={"input": (1.25, "measured"), "output": (2.0, "measured")})]
        )
        entry = monitor.detect_changes(previous, current, CHANGE_CONFIG)["rate_changed"]["cline-pass/a"]
        self.assertEqual(["input"], sorted(entry["classes"]))
        self.assertAlmostEqual(0.25, entry["classes"]["input"]["change_ratio"], places=10)
        self.assertNotIn("provenance_change", entry["classes"]["input"])

    def test_immaterial_rate_changes_are_ignored(self):
        previous = snapshot([model("cline-pass/a", rates={"input": (1.0, "measured")})])
        current = snapshot([model("cline-pass/a", rates={"input": (1.005, "measured")})])
        self.assertEqual(
            {}, monitor.detect_changes(previous, current, CHANGE_CONFIG)["rate_changed"]
        )

    def test_a_provenance_switch_is_flagged_as_not_directly_comparable(self):
        previous = snapshot([model("cline-pass/a", rates={"input": (1.4, "reference")})])
        current = snapshot([model("cline-pass/a", rates={"input": (1.55, "measured")})])
        change = monitor.detect_changes(previous, current, CHANGE_CONFIG)["rate_changed"][
            "cline-pass/a"
        ]["classes"]["input"]
        self.assertEqual({"from": "reference", "to": "measured"}, change["provenance_change"])
        self.assertIn("not directly comparable", change["note"])

    def test_classes_unavailable_on_either_side_are_not_compared(self):
        previous = snapshot([model("cline-pass/a", rates={"input": (1.0, "measured")})])
        current = snapshot([model("cline-pass/a", rates={"input": (None, None)})])
        self.assertEqual({}, monitor.detect_changes(previous, current, CHANGE_CONFIG)["rate_changed"])


class ObservedBlendDeltaTests(unittest.TestCase):
    def test_drift_above_ten_percent_is_reported(self):
        previous = snapshot([model("cline-pass/a", observed=1.0, name="Model A")])
        current = snapshot([model("cline-pass/a", observed=1.25, name="Model A")])
        drift = monitor.detect_changes(previous, current, CHANGE_CONFIG)["observed_blend_changed"]
        self.assertIn("cline-pass/a", drift)
        self.assertAlmostEqual(0.25, drift["cline-pass/a"]["change_ratio"], places=10)

    def test_drift_below_ten_percent_is_ignored(self):
        previous = snapshot([model("cline-pass/a", observed=1.0)])
        current = snapshot([model("cline-pass/a", observed=1.05)])
        self.assertEqual(
            {}, monitor.detect_changes(previous, current, CHANGE_CONFIG)["observed_blend_changed"]
        )

    def test_missing_observations_are_not_compared(self):
        previous = snapshot([model("cline-pass/a", observed=None)])
        current = snapshot([model("cline-pass/a", observed=2.0)])
        self.assertEqual(
            {}, monitor.detect_changes(previous, current, CHANGE_CONFIG)["observed_blend_changed"]
        )


class SummaryTests(unittest.TestCase):
    def test_deterministic_summary_reports_only_what_changed(self):
        previous = snapshot([model("cline-pass/a", 70.0, {"input": (1.0, "measured")}, name="A")])
        current = snapshot(
            [
                model("cline-pass/a", 71.0, {"input": (1.2, "measured")}, name="A"),
                model("cline-pass/b", name="B"),
            ]
        )
        changes = monitor.detect_changes(previous, current, CHANGE_CONFIG)
        summary = monitor.deterministic_summary(changes)
        self.assertIn("cline-pass/b", summary)
        self.assertIn("1 AA quality change", summary)
        self.assertIn("1 rate change", summary)

    def test_no_changes_produces_an_explicit_statement(self):
        same = snapshot([model("cline-pass/a", 70.0)])
        summary = monitor.deterministic_summary(
            monitor.detect_changes(same, same, CHANGE_CONFIG)
        )
        self.assertIn("No catalog, quality, or billing-rate changes", summary)

    def test_the_summary_respects_the_word_cap(self):
        models = [
            model("cline-pass/m%03d" % index, name="Model %03d" % index) for index in range(80)
        ]
        changes = monitor.detect_changes(snapshot([model("x")]), snapshot(models), CHANGE_CONFIG)
        capped = monitor.deterministic_summary(changes, 20)
        self.assertLessEqual(len(capped.replace("...", "").split()), 20)
        self.assertTrue(capped.endswith("..."))
        self.assertTrue(monitor.deterministic_summary(changes, 500).endswith("."))


if __name__ == "__main__":
    unittest.main()