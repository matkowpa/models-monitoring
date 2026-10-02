"""Narrative chat parsing and the best-value fallback.

Both behaviours were pinned from a real live run: ClinePass chat completions
arrive wrapped in the ``{ data, success }`` envelope, and a $0 free model
dominates the Pareto set so the best-value card has to fall back to the best
*paid* model instead of rendering "no eligible model".
"""

import json
import unittest

from tests.support import ROOT, FixtureTestCase, config
from src import monitor, run, site


class ChatPayloadTests(unittest.TestCase):
    def test_a_plain_openai_reply_is_read(self):
        payload = {"choices": [{"message": {"content": "Two models changed."}}]}
        self.assertEqual("Two models changed.", run.extract_summary_text(payload))

    def test_the_data_success_envelope_is_unwrapped(self):
        # The shape observed from ClinePass on 2026-10-02.
        payload = {
            "data": {"choices": [{"message": {"content": "Weekly summary text."}}]},
            "success": True,
        }
        self.assertEqual("Weekly summary text.", run.extract_summary_text(payload))

    def test_a_reasoning_only_reply_is_used_rather_than_discarded(self):
        payload = {
            "data": {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning": "Reasoned summary.",
                            "reasoning_content": "Reasoned summary.",
                        }
                    }
                ]
            },
            "success": True,
        }
        self.assertEqual("Reasoned summary.", run.extract_summary_text(payload))

    def test_content_parts_are_joined(self):
        payload = {
            "choices": [
                {"message": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}}
            ]
        }
        self.assertEqual("a b", run.extract_summary_text(payload))

    def test_unusable_payloads_return_none(self):
        for payload in (
            {},
            None,
            {"choices": []},
            {"data": {"choices": []}, "success": True},
            {"success": True},
            {"data": None, "success": False},
        ):
            self.assertIsNone(run.extract_summary_text(payload), repr(payload))

    def test_a_failed_envelope_is_not_read_as_a_reply(self):
        payload = {"data": None, "error": "unauthorized", "success": False}
        self.assertIsNone(run.extract_summary_text(payload))


class LiveSnapshotRegressionTests(unittest.TestCase):
    """Assertions against the checked-in live snapshot, when it is present."""

    def setUp(self):
        path = ROOT / "data" / "history" / "2026-10-02.json"
        if not path.exists():
            self.skipTest("live snapshot not present in this checkout")
        self.snapshot = json.loads(path.read_text(encoding="utf-8"))

    def test_the_live_run_did_not_need_a_money_scale_warning(self):
        self.assertEqual([], self.snapshot["report"]["warnings"])

    def test_measured_rates_agree_with_the_published_reference_table(self):
        # The fitted glm-5.3 rates matched the published card exactly on the live
        # data, which is the strongest available signal that the money-scale
        # inference and the token-class algebra are both right.
        model = monitor.models_by_id(self.snapshot["models"])["cline-pass/glm-5.3"]
        for rate_class, expected in (("input", 1.4), ("output", 4.4), ("cache_read", 0.26)):
            self.assertEqual(
                monitor.PROVENANCE_MEASURED,
                monitor.rate_provenance(model["rates"], rate_class),
            )
            self.assertAlmostEqual(
                expected, monitor.rate_value(model["rates"], rate_class), places=6
            )

    def test_every_priced_rate_field_states_its_provenance(self):
        for model in self.snapshot["models"]:
            for rate_class in monitor.RATE_CLASSES:
                entry = model["rates"][rate_class]
                self.assertIn("provenance", entry)
                if entry.get("usd_per_mtok") is not None:
                    self.assertIsNotNone(entry["provenance"], (model["id"], rate_class))

    def test_free_models_are_declared_zero_and_paid_models_are_not(self):
        for model in self.snapshot["models"]:
            if model["free"]:
                self.assertEqual(
                    monitor.PROVENANCE_CATALOG_FREE,
                    monitor.rate_provenance(model["rates"], "input"),
                )
                self.assertEqual(0.0, model["cost_usd"]["planning"])
            else:
                self.assertNotEqual(
                    monitor.PROVENANCE_CATALOG_FREE,
                    monitor.rate_provenance(model["rates"], "input"),
                    model["id"],
                )

    def test_every_model_with_a_quality_score_and_a_cost_is_charted_exactly_once(self):
        # The plan plots one point per model with a quality score and at least one
        # profile cost, so a scored model with no usable rates is listed in the
        # table but deliberately absent from the chart.
        chartable = sorted(
            model["id"]
            for model in self.snapshot["models"]
            if model["quality"] and any(value is not None for value in model["cost_usd"].values())
        )
        charted = sorted(
            [point["id"] for point in self.snapshot["chart"]["points"]]
            + [point["id"] for point in self.snapshot["chart"]["free_band"]]
        )
        self.assertEqual(chartable, charted)

    def test_a_scored_model_without_any_cost_is_not_charted(self):
        charted = {
            point["id"]
            for point in self.snapshot["chart"]["points"] + self.snapshot["chart"]["free_band"]
        }
        for model in self.snapshot["models"]:
            if model["quality"] and all(value is None for value in model["cost_usd"].values()):
                self.assertNotIn(model["id"], charted, model["id"])

    def test_every_fit_rejection_states_a_reason(self):
        for model_id, reason in (self.snapshot["observation"]["fit_rejections"] or {}).items():
            self.assertTrue(reason, model_id)


class BestValueFallbackTests(FixtureTestCase):
    """The best-value card must name a paid model even when $0 models dominate."""

    def setUp(self):
        super().setUp()
        self.entries = [
            {"id": "cline-pass/paid-a", "slug": "paid-a", "name": "Paid A", "free": False},
            {"id": "cline-pass/paid-b", "slug": "paid-b", "name": "Paid B", "free": False},
            {"id": "cline-free/free-1", "slug": "free-1", "name": "Free 1", "free": True},
        ]

        def rates(input_rate, output_rate, free=False):
            record = {"catalog_free": free}
            for name, value in (
                ("input", input_rate),
                ("output", output_rate),
                ("cache_read", input_rate),
                ("cache_write", input_rate),
            ):
                record[name] = {"usd_per_mtok": value, "provenance": monitor.PROVENANCE_MEASURED}
            return record

        self.rates = {
            "cline-pass/paid-a": rates(1.0, 2.0),
            "cline-pass/paid-b": rates(2.0, 4.0),
            "cline-free/free-1": rates(0.0, 0.0, free=True),
        }
        self.quality = {
            "cline-pass/paid-a": {"intelligence_index": 60.0},
            "cline-pass/paid-b": {"intelligence_index": 75.0},
            # The live case: the free model was the highest-quality model in the
            # catalog, so at $0 it dominated every paid model.
            "cline-free/free-1": {"intelligence_index": 90.0},
        }

    def report(self):
        return monitor.score_models(self.entries, self.rates, self.quality, {}, {}, self.config)

    def test_the_only_pareto_member_is_the_free_model(self):
        report = self.report()
        models = monitor.models_by_id(report["models"])
        self.assertEqual(
            ["cline-free/free-1"],
            [model["id"] for model in report["models"] if model["pareto"]["planning"]],
        )
        self.assertIsNone(models["cline-free/free-1"]["efficiency"]["planning"])

    def test_the_card_still_names_the_best_paid_model_and_says_why(self):
        report = self.report()
        winner = report["best_value"]["planning"]
        # paid-a costs 0.256 USD and paid-b 0.512 USD for planning, so the median
        # is 0.384: paid-a earns 2.9 points for being half the median cost and
        # paid-b loses 2.1 for being above it, which still leaves paid-b ahead.
        self.assertEqual("cline-pass/paid-b", winner["id"])
        self.assertAlmostEqual(72.92, winner["efficiency"], places=2)
        self.assertEqual("highest_efficiency_paid", winner["basis"])
        html = site.render_dashboard(
            {
                "report": {},
                "stats": report["stats"],
                "medians": report["medians"],
                "best_value": report["best_value"],
                "chart": report["chart"],
                "models": report["models"],
                "changes": {},
                "summary": "x",
                "reference_rates": {},
            },
            self.config,
        )
        self.assertIn("best paid model", html)
        self.assertNotIn("no eligible model", html)

    def test_without_free_models_the_card_uses_the_pareto_set(self):
        report = monitor.score_models(
            self.entries[:2],
            {
                "cline-pass/paid-a": self.rates["cline-pass/paid-a"],
                "cline-pass/paid-b": self.rates["cline-pass/paid-b"],
            },
            {
                "cline-pass/paid-a": self.quality["cline-pass/paid-a"],
                "cline-pass/paid-b": self.quality["cline-pass/paid-b"],
            },
            {},
            {},
            self.config,
        )
        self.assertEqual("highest_efficiency_pareto", report["best_value"]["planning"]["basis"])


if __name__ == "__main__":
    unittest.main()