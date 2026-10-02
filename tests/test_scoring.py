"""Task cost, efficiency, Pareto, and best value (section 7 step 5)."""

import unittest

from tests.support import config
from src import monitor


def rates(input_rate, output_rate, cache_read=None, cache_write=None, free=False):
    record = {"catalog_free": free}
    for name, value in (
        ("input", input_rate),
        ("output", output_rate),
        ("cache_read", cache_read),
        ("cache_write", cache_write),
    ):
        record[name] = {"usd_per_mtok": value, "provenance": monitor.PROVENANCE_MEASURED}
    return record


class TaskCostTests(unittest.TestCase):
    def setUp(self):
        self.profiles = config()["profiles"]

    def test_cost_matches_a_hand_calculated_example(self):
        # (200000*0.5 + 30000*1 + 10000*1 + 8000*2) / 1e6 = 0.156 USD
        cost = monitor.task_cost(rates(1.0, 2.0, 0.5, 1.0), self.profiles["planning"])
        self.assertAlmostEqual(0.156, cost, places=10)

    def test_verbosity_multiplier_is_applied(self):
        cost = monitor.task_cost(rates(1.0, 2.0, 0.5, 1.0), self.profiles["planning"], 2.0)
        self.assertAlmostEqual(0.312, cost, places=10)

    def test_missing_cache_rates_fall_back_to_the_input_rate(self):
        with_cache = monitor.task_cost(rates(1.0, 2.0, 1.0, 1.0), self.profiles["execution"])
        without_cache = monitor.task_cost(rates(1.0, 2.0, None, None), self.profiles["execution"])
        self.assertAlmostEqual(with_cache, without_cache, places=12)

    def test_an_explicitly_zero_cache_rate_means_free_cache_tokens(self):
        free_cache = monitor.task_cost(rates(1.0, 2.0, 0.0, 0.0), self.profiles["planning"])
        paid_cache = monitor.task_cost(rates(1.0, 2.0, 1.0, 1.0), self.profiles["planning"])
        self.assertLess(free_cache, paid_cache)
        self.assertAlmostEqual((30000 * 1.0 + 8000 * 2.0) / 1e6, free_cache, places=10)

    def test_zero_base_rates_make_a_paid_model_unavailable(self):
        self.assertIsNone(monitor.task_cost(rates(0.0, 2.0), self.profiles["planning"]))
        self.assertIsNone(monitor.task_cost(rates(1.0, 0.0), self.profiles["planning"]))
        self.assertIsNone(monitor.task_cost(rates(None, 2.0), self.profiles["planning"]))

    def test_catalog_free_models_cost_zero_rather_than_being_unavailable(self):
        free_rates = rates(0.0, 0.0, 0.0, 0.0, free=True)
        self.assertEqual(0.0, monitor.task_cost(free_rates, self.profiles["planning"]))
        self.assertTrue(monitor.base_rates_usable(free_rates))


class EfficiencyTests(unittest.TestCase):
    def test_median_ignores_unavailable_and_zero_costs(self):
        self.assertEqual(2.0, monitor.price_median([1.0, 2.0, 3.0, None, 0.0]))
        self.assertIsNone(monitor.price_median([None, 0.0]))

    def test_a_model_at_the_median_scores_its_quality(self):
        self.assertEqual(70.0, monitor.efficiency(70.0, 2.0, 2.0, 5))

    def test_penalties_are_five_and_eight_points_per_cost_doubling(self):
        self.assertAlmostEqual(65.0, monitor.efficiency(70.0, 4.0, 2.0, 5), places=10)
        self.assertAlmostEqual(62.0, monitor.efficiency(70.0, 4.0, 2.0, 8), places=10)
        self.assertAlmostEqual(75.0, monitor.efficiency(70.0, 1.0, 2.0, 5), places=10)

    def test_zero_cost_models_have_no_efficiency(self):
        self.assertIsNone(monitor.efficiency(70.0, 0.0, 2.0, 5))

    def test_missing_inputs_yield_no_efficiency(self):
        self.assertIsNone(monitor.efficiency(None, 2.0, 2.0, 5))
        self.assertIsNone(monitor.efficiency(70.0, None, 2.0, 5))
        self.assertIsNone(monitor.efficiency(70.0, 2.0, None, 5))


class ParetoTests(unittest.TestCase):
    def test_dominated_points_are_excluded(self):
        points = [
            {"id": "cheap-low", "quality": 50, "cost": 1.0},
            {"id": "pricey-high", "quality": 80, "cost": 3.0},
            {"id": "worst", "quality": 40, "cost": 5.0},
        ]
        members = monitor.pareto_members(points)
        self.assertTrue(members["cheap-low"])
        self.assertTrue(members["pricey-high"])
        self.assertFalse(members["worst"])

    def test_equal_points_do_not_dominate_each_other(self):
        members = monitor.pareto_members(
            [{"id": "a", "quality": 60, "cost": 2.0}, {"id": "b", "quality": 60, "cost": 2.0}]
        )
        self.assertTrue(members["a"] and members["b"])

    def test_zero_cost_models_stay_in_the_set(self):
        members = monitor.pareto_members(
            [{"id": "free", "quality": 55, "cost": 0.0}, {"id": "paid", "quality": 80, "cost": 2.0}]
        )
        self.assertTrue(members["free"])
        self.assertTrue(members["paid"])

    def test_lower_quality_free_model_is_dominated_by_a_better_free_model(self):
        members = monitor.pareto_members(
            [
                {"id": "free-low", "quality": 50, "cost": 0.0},
                {"id": "free-high", "quality": 70, "cost": 0.0},
            ]
        )
        self.assertFalse(members["free-low"])
        self.assertTrue(members["free-high"])


class ScoreModelsTests(unittest.TestCase):
    def setUp(self):
        self.config = config()
        self.entries = [
            {"id": "cline-pass/strong", "slug": "strong", "name": "Strong", "free": False},
            {"id": "cline-pass/cheap", "slug": "cheap", "name": "Cheap", "free": False},
            {"id": "cline-pass/weak", "slug": "weak", "name": "Weak", "free": False},
            {"id": "cline-free/zero", "slug": "zero", "name": "Zero", "free": True},
        ]
        self.rates = {
            "cline-pass/strong": rates(4.0, 8.0, 4.0, 4.0),
            "cline-pass/cheap": rates(0.4, 0.8, 0.4, 0.4),
            "cline-pass/weak": rates(2.0, 4.0, 2.0, 2.0),
            "cline-free/zero": rates(0.0, 0.0, 0.0, 0.0, free=True),
        }
        self.quality = {
            "cline-pass/strong": {"intelligence_index": 80.0, "coding_index": 75.0},
            "cline-pass/cheap": {"intelligence_index": 60.0, "coding_index": 58.0},
            "cline-pass/weak": {"intelligence_index": 40.0, "coding_index": 38.0},
            "cline-free/zero": {"intelligence_index": 50.0, "coding_index": 49.0},
        }
        self.report = monitor.score_models(
            self.entries, self.rates, self.quality, {}, {}, self.config
        )

    def model(self, model_id):
        return monitor.models_by_id(self.report["models"])[model_id]

    def test_every_catalog_entry_appears_once(self):
        ids = [model["id"] for model in self.report["models"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(4, len(ids))

    def test_free_models_are_excluded_from_the_median(self):
        # Planning profile costs: strong 1.024, weak 0.512, cheap 0.1024 USD, so
        # the median is 0.512; the zero-cost model must not drag it down.
        self.assertAlmostEqual(0.512, self.report["medians"]["planning"], places=10)
        self.assertAlmostEqual(
            0.1024, self.model("cline-pass/cheap")["cost_usd"]["planning"], places=10
        )

    def test_free_models_get_no_efficiency_but_stay_in_the_pareto_set(self):
        free = self.model("cline-free/zero")
        self.assertEqual(0.0, free["cost_usd"]["planning"])
        self.assertIsNone(free["efficiency"]["planning"])
        self.assertTrue(free["pareto"]["planning"])

    def test_best_value_names_the_best_paid_model(self):
        # strong costs exactly twice the planning median (5 points lost) and the
        # cheap model earns 11.6 points for costing a fifth of it, so the winner
        # differs per profile and is always a paid model.
        self.assertEqual("cline-pass/strong", self.report["best_value"]["planning"]["id"])
        self.assertAlmostEqual(
            75.0, self.report["best_value"]["planning"]["efficiency"], places=6
        )
        self.assertEqual("cline-pass/cheap", self.report["best_value"]["execution"]["id"])
        self.assertAlmostEqual(
            78.58, self.report["best_value"]["execution"]["efficiency"], places=2
        )

    def test_there_is_no_quality_gate(self):
        self.assertIsNotNone(self.model("cline-pass/weak")["efficiency"]["planning"])

    def test_models_without_a_quality_score_get_no_efficiency(self):
        report = monitor.score_models(self.entries, self.rates, {}, {}, {}, self.config)
        weak = monitor.models_by_id(report["models"])["cline-pass/weak"]
        self.assertIsNone(weak["efficiency"]["planning"])
        self.assertFalse(weak["pareto"]["planning"])
        self.assertIsNone(report["best_value"]["planning"])

    def test_chart_plots_each_model_once_and_puts_free_models_in_the_band(self):
        plotted = [point["id"] for point in self.report["chart"]["points"]]
        band = [point["id"] for point in self.report["chart"]["free_band"]]
        self.assertEqual(3, len(plotted))
        self.assertIn("cline-free/zero", band)
        self.assertNotIn("cline-free/zero", plotted)
        self.assertEqual(len(plotted), len(set(plotted)))
        for model_id in ("cline-pass/cheap", "cline-pass/strong", "cline-free/zero"):
            point = next(
                item
                for item in self.report["chart"]["points"] + self.report["chart"]["free_band"]
                if item["id"] == model_id
            )
            self.assertTrue(point["frontier"], model_id)

    def test_a_top_quality_free_model_does_not_empty_the_paid_frontier(self):
        # Live-run regression: the catalog-free model has the highest quality at
        # $0, which dominates every paid model when free models join the chart
        # dominance check. The plotted frontier must stay non-empty so the
        # Pareto curve keeps rendering; the free model stays non-dominated too.
        entries = [
            {"id": "cline-pass/strong", "slug": "strong", "name": "Strong", "free": False},
            {"id": "cline-pass/worse", "slug": "worse", "name": "Worse", "free": False},
            {"id": "cline-free/champ", "slug": "champ", "name": "Champ", "free": True},
        ]
        report = monitor.score_models(
            entries,
            {
                "cline-pass/strong": rates(4.0, 8.0, 4.0, 4.0),
                "cline-pass/worse": rates(8.0, 16.0, 8.0, 8.0),
                "cline-free/champ": rates(0.0, 0.0, 0.0, 0.0, free=True),
            },
            {
                "cline-pass/strong": {"intelligence_index": 80.0},
                "cline-pass/worse": {"intelligence_index": 40.0},
                "cline-free/champ": {"intelligence_index": 95.0},
            },
            {},
            {},
            self.config,
        )
        chart = report["chart"]
        plotted = {point["id"]: point for point in chart["points"]}
        self.assertTrue(plotted["cline-pass/strong"]["frontier"], "the paid champion stays on the frontier")
        self.assertFalse(plotted["cline-pass/worse"]["frontier"])
        band = {point["id"]: point for point in chart["free_band"]}
        self.assertTrue(band["cline-free/champ"]["frontier"])

    def test_average_cost_is_the_mean_of_the_available_profile_costs(self):
        entries = [{"id": "cline-pass/cheap", "slug": "cheap", "name": "Cheap", "free": False}]
        report = monitor.score_models(
            entries,
            {"cline-pass/cheap": rates(0.4, 0.8, 0.4, 0.4)},
            {"cline-pass/cheap": {"intelligence_index": 60.0}},
            {},
            {},
            self.config,
        )
        point = report["chart"]["points"][0]
        self.assertEqual(["execution", "planning"], point["profiles"])
        self.assertAlmostEqual((0.1024 + 0.196) / 2.0, point["cost"], places=10)

    def test_a_single_profile_configuration_plots_that_cost_alone(self):
        single = dict(self.config)
        single["profiles"] = {"planning": self.config["profiles"]["planning"]}
        entries = [{"id": "cline-pass/cheap", "slug": "cheap", "name": "Cheap", "free": False}]
        report = monitor.score_models(
            entries,
            {"cline-pass/cheap": rates(0.4, 0.8, 0.4, 0.4)},
            {"cline-pass/cheap": {"intelligence_index": 60.0}},
            {},
            {},
            single,
        )
        point = report["chart"]["points"][0]
        self.assertEqual(["planning"], point["profiles"])
        self.assertAlmostEqual(0.1024, point["cost"], places=10)

    def test_stats_render_unavailable_values_instead_of_zero(self):
        entry = {"id": "cline-pass/x", "slug": "x", "name": "X", "free": False}
        report = monitor.score_models(
            [entry],
            {"cline-pass/x": monitor.resolve_rates(entry, None)},
            {},
            {},
            {},
            self.config,
        )
        stats = report["stats"]
        self.assertEqual(1, stats["discovered"])
        self.assertEqual(0, stats["priced"])
        self.assertEqual(0, stats["benchmarked"])
        self.assertIsNone(stats["highest_quality"])
        self.assertIsNone(report["models"][0]["cost_usd"]["planning"])


class BlendedRateTests(unittest.TestCase):
    def test_blended_rate_uses_the_eighty_twenty_weights(self):
        self.assertAlmostEqual(
            0.8 * 1.0 + 0.2 * 2.0, monitor.blended_input_output(rates(1.0, 2.0))
        )
        self.assertIsNone(monitor.blended_input_output(rates(1.0, None)))


if __name__ == "__main__":
    unittest.main()
