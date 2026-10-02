"""Rate fitting, provenance, and the money-scale guard (section 7 step 3)."""

import unittest
from datetime import date, timedelta

from tests.support import config, ground_truth, load, normalized_fixture_rows
from src import monitor

REFERENCE_META = load("clinepass_reference_rates.json")


def paid_entry(slug):
    return {
        "id": "cline-pass/%s" % slug,
        "slug": slug,
        "name": slug,
        "free": False,
    }


class FitRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.rows, _dropped = normalized_fixture_rows()
        self.fit_config = config()["rate_fit"]
        end = max(row["date"] for row in self.rows)
        self.end = end
        self.start = (date.fromisoformat(end) - timedelta(days=29)).isoformat()

    def fit(self, slug):
        selected = monitor.rows_for_model(self.rows, paid_entry(slug))
        return monitor.fit_rates(selected, self.fit_config, self.start, self.end)

    def test_fit_recovers_the_ground_truth_rates_for_every_measured_model(self):
        for slug, values in ground_truth().items():
            fit = self.fit(slug)
            self.assertEqual(monitor.FIT_ACCEPTED, fit["status"], "%s: %s" % (slug, fit["reason"]))
            for rate_class, expected in (
                ("input", values[0]),
                ("output", values[1]),
                ("cache_read", values[2]),
            ):
                self.assertAlmostEqual(
                    expected, fit["rates"][rate_class], places=5, msg="%s %s" % (slug, rate_class)
                )

    def test_fit_reports_its_own_statistics(self):
        fit = self.fit("glm-5.3")
        self.assertEqual(60, fit["rows"])
        self.assertEqual(60, fit["rows_in_window"])
        self.assertEqual(0, fit["dropped_rows"])
        self.assertLess(fit["total_reconciliation_error"], 0.01)
        self.assertLess(fit["rms_residual_usd"], fit["allowed_rms_residual_usd"])
        self.assertGreater(fit["condition_number"], 1.0)
        self.assertEqual(self.start, fit["window_start"])
        self.assertEqual(self.end, fit["window_end"])

    def test_rate_window_excludes_older_rows_with_different_rates(self):
        # glm-5.3 also has 20 rows dated 45+ days back at doubled rates; if the
        # window filter were ignored the fit would be rejected.
        selected = monitor.rows_for_model(self.rows, paid_entry("glm-5.3"))
        self.assertEqual(80, len(selected))
        fit = monitor.fit_rates(selected, self.fit_config, self.start, self.end)
        self.assertEqual(60, fit["rows_in_window"])
        self.assertEqual(monitor.FIT_ACCEPTED, fit["status"])

    def test_old_rows_exist_and_would_fit_different_rates(self):
        # glm-5.3's fixture includes 20 rows dated 45-64 days back at doubled
        # rates. Fitting exactly that stretch must recover the doubled rates,
        # which proves the window filter selects rows rather than ignoring them.
        selected = monitor.rows_for_model(self.rows, paid_entry("glm-5.3"))
        old_start = (date.fromisoformat(self.start) - timedelta(days=35)).isoformat()
        old_end = (date.fromisoformat(self.start) - timedelta(days=16)).isoformat()
        fit = monitor.fit_rates(selected, self.fit_config, old_start, old_end)
        self.assertEqual(20, fit["rows_in_window"])
        self.assertEqual(monitor.FIT_ACCEPTED, fit["status"], fit["reason"])
        self.assertAlmostEqual(3.10, fit["rates"]["input"], places=5)

    def test_rows_outside_the_window_alone_cannot_be_fitted(self):
        selected = monitor.rows_for_model(self.rows, paid_entry("glm-5.3"))
        old_end = (date.fromisoformat(self.start) - timedelta(days=40)).isoformat()
        old_start = (date.fromisoformat(old_end) - timedelta(days=4)).isoformat()
        fit = monitor.fit_rates(selected, self.fit_config, old_start, old_end)
        self.assertEqual(0, fit["rows_in_window"])
        self.assertEqual(monitor.FIT_REJECTED, fit["status"])
        self.assertIn("minimum", fit["reason"])


class FitRejectionTests(unittest.TestCase):
    def setUp(self):
        self.fit_config = config()["rate_fit"]

    def rows(self, mixes, costs):
        rows = []
        for index, (fresh, completion, cached) in enumerate(mixes):
            rows.append(
                {
                    "date": "2026-10-01",
                    "cost_usd": costs[index],
                    "total_tokens": fresh + completion + cached,
                    "fresh_input_tokens": fresh,
                    "completion_tokens": completion,
                    "cached_tokens": cached,
                }
            )
        return rows

    def test_rank_deficient_token_mix_is_rejected(self):
        fit = monitor.fit_rates(self.rows([(100, 100, 800)] * 20, [0.01] * 20), self.fit_config)
        self.assertEqual(monitor.FIT_REJECTED, fit["status"])
        self.assertIn("condition number", fit["reason"])

    def test_too_few_rows_is_rejected(self):
        fit = monitor.fit_rates(self.rows([(100, 100, 800)] * 5, [0.01] * 5), self.fit_config)
        self.assertEqual(monitor.FIT_REJECTED, fit["status"])
        self.assertIn("minimum", fit["reason"])

    def test_cost_free_rows_are_dropped_and_counted(self):
        fit = monitor.fit_rates(self.rows([(100, 100, 0)] * 3, [0.0] * 3), self.fit_config)
        self.assertEqual(0, fit["rows"])
        self.assertEqual(3, fit["dropped_rows"])

    def test_a_fit_that_needs_a_negative_rate_is_rejected(self):
        # Output-only rows cost more than rows with the same output plus fresh
        # input, so no non-negative input rate explains the data.
        mixes = [(0, 1000, 0)] * 8 + [(1000, 1000, 0)] * 8 + [(0, 0, 5000)] * 8
        costs = [0.005] * 8 + [0.004] * 8 + [0.0005] * 8
        fit = monitor.fit_rates(self.rows(mixes, costs), self.fit_config)
        self.assertEqual(monitor.FIT_REJECTED, fit["status"])
        self.assertTrue(
            "strictly positive" in fit["reason"] or "non-negative" in fit["reason"],
            fit["reason"],
        )

    def test_rejected_fits_publish_no_rates(self):
        fit = monitor.fit_rates(self.rows([(100, 100, 800)] * 20, [0.01] * 20), self.fit_config)
        self.assertIsNone(fit["rates"]["input"])
        self.assertIsNone(fit["total_reconciliation_error"])


class RateResolutionTests(unittest.TestCase):
    def setUp(self):
        self.reference_models = REFERENCE_META["models"]
        self.reference_meta = {
            "source_name": REFERENCE_META["source_name"],
            "source_url": REFERENCE_META["source_url"],
            "retrieved_at": REFERENCE_META["retrieved_at"],
        }

    def test_free_entry_is_declared_zero_and_never_fitted(self):
        record = monitor.resolve_rates({"id": "cline-free/x", "slug": "x", "free": True}, None)
        for rate_class in monitor.RATE_CLASSES:
            self.assertEqual(0.0, record[rate_class]["usd_per_mtok"])
            self.assertEqual(monitor.PROVENANCE_CATALOG_FREE, record[rate_class]["provenance"])
        profile = config()["profiles"]["planning"]
        self.assertEqual(0.0, monitor.task_cost(record, profile))

    def test_rejected_fit_falls_back_to_the_reference_table_alone(self):
        record = monitor.resolve_rates(
            paid_entry("glm-5.3"),
            monitor.empty_fit(reason="not estimable"),
            self.reference_models["glm-5.3"],
            self.reference_meta,
        )
        for rate_class in monitor.FIT_CLASSES:
            self.assertEqual(monitor.PROVENANCE_REFERENCE, record[rate_class]["provenance"])
        self.assertEqual(1.4, record["input"]["usd_per_mtok"])
        self.assertEqual(self.reference_meta["source_url"], record["input"]["source_url"])

    def test_measured_fit_supplies_all_three_fitted_classes(self):
        # Three independent token mixes at known rates, so the fit is
        # identifiable and the record must carry measured provenance with the
        # window and row count behind it.
        mixes = ((1000, 500, 8500), (3000, 1200, 2000), (500, 900, 12000))
        known = (1.0, 2.0, 0.1)
        rows = [
            {
                "date": "2026-10-01",
                "cost_usd": (fresh * known[0] + out * known[1] + cached * known[2]) / 1e6,
                "total_tokens": fresh + out + cached,
                "fresh_input_tokens": fresh,
                "completion_tokens": out,
                "cached_tokens": cached,
            }
            for fresh, out, cached in mixes * 5
        ]
        fit = monitor.fit_rates(rows, config()["rate_fit"], "2026-09-01", "2026-10-02")
        self.assertEqual(monitor.FIT_ACCEPTED, fit["status"], fit.get("reason"))
        record = monitor.resolve_rates(paid_entry("glm-5.3"), fit, {}, self.reference_meta)
        provenances = {record[rate_class]["provenance"] for rate_class in monitor.FIT_CLASSES}
        self.assertEqual({monitor.PROVENANCE_MEASURED}, provenances)
        self.assertEqual(fit["rows"], record["input"]["rows"])
        self.assertEqual(fit["window_start"], record["input"]["window_start"])
        self.assertAlmostEqual(1.0, record["input"]["usd_per_mtok"], places=5)

    def test_cache_write_uses_the_reference_value_when_it_exists(self):
        record = monitor.resolve_rates(
            paid_entry("qwen3.7-max"),
            monitor.empty_fit(reason="not estimable"),
            self.reference_models["qwen3.7-max"],
            self.reference_meta,
        )
        self.assertEqual(3.125, record["cache_write"]["usd_per_mtok"])
        self.assertEqual(monitor.PROVENANCE_REFERENCE, record["cache_write"]["provenance"])

    def test_cache_write_falls_back_to_the_input_rate_and_says_so(self):
        record = monitor.resolve_rates(
            paid_entry("glm-5.3"),
            monitor.empty_fit(reason="not estimable"),
            self.reference_models["glm-5.3"],
            self.reference_meta,
        )
        self.assertEqual(1.4, record["cache_write"]["usd_per_mtok"])
        self.assertEqual(monitor.PROVENANCE_INPUT_FALLBACK, record["cache_write"]["provenance"])
        self.assertIn("cache-write", record["cache_write"]["note"])

    def test_classes_stay_unavailable_when_neither_source_prices_them(self):
        record = monitor.resolve_rates(paid_entry("unknown-model"), None, {}, self.reference_meta)
        self.assertIsNone(record["input"]["usd_per_mtok"])
        self.assertIsNone(record["cache_write"]["usd_per_mtok"])
        self.assertFalse(monitor.base_rates_usable(record))
        self.assertIsNone(monitor.task_cost(record, config()["profiles"]["planning"]))


class MoneyScaleGuardTests(unittest.TestCase):
    def test_plausible_measured_rates_do_not_warn(self):
        fits = {
            "glm-5.3": {
                "status": monitor.FIT_ACCEPTED,
                "rates": {"input": 1.5, "output": 4.5, "cache_read": 0.28},
            }
        }
        self.assertIsNone(monitor.money_scale_warning(fits, REFERENCE_META["models"]))

    def test_implausible_rates_warn_about_the_money_scales(self):
        fits = {
            "glm-5.3": {
                "status": monitor.FIT_ACCEPTED,
                "rates": {"input": 1550000.0, "output": 4850000.0, "cache_read": 290000.0},
            }
        }
        warning = monitor.money_scale_warning(fits, REFERENCE_META["models"])
        self.assertIsNotNone(warning)
        self.assertIn("costUsd", warning)

    def test_no_measured_fits_means_no_warning(self):
        self.assertIsNone(monitor.money_scale_warning({}, REFERENCE_META["models"]))


if __name__ == "__main__":
    unittest.main()
