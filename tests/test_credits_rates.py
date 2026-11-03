"""Fitting rates from credit-billed usage rows.

A model Cline serves only through credits has no ClinePass rows, so the
subscription fit sees nothing and the report used to publish an empty row even
when the account had thousands of billed requests for it. Credit rows are
fittable, but their individual costs carry a flat or rounded component: on live
data the fit explains 98.5% of the variance and reconciles within 0.3% while the
worst single row misses by 90%, so a per-row RMS gate rejects a perfectly good
price level. The credits profile therefore drops the RMS gate and judges the
median relative error plus the aggregate reconciliation instead.
"""

import random
import unittest

from src import monitor, run, site

STRICT = {
    "min_rows": 12,
    "max_condition_number": 1000000.0,
    "max_total_reconciliation_error": 0.01,
    "rms_residual_ratio": 0.02,
    "rms_residual_floor_usd": 0.00002,
}
CREDITS = {
    "min_rows": 12,
    "max_condition_number": 1000000.0,
    "max_total_reconciliation_error": 0.01,
    "rms_residual_ratio": None,
    "max_median_relative_error": 0.05,
}
TRUE_RATES = (0.15, 0.60, 0.003)
WINDOW = ("2026-10-01", "2026-10-31")


def usage_rows(
    count=30,
    rates=TRUE_RATES,
    deltas=None,
    doubles=(),
    seed=7,
    billing_type="cline-usage",
    model="x",
):
    """Rows whose cost is exactly linear in the token classes, plus perturbations."""
    rnd = random.Random(seed)
    rows = []
    for index in range(count):
        fresh = rnd.randrange(5_000, 90_000)
        completion = rnd.randrange(500, 9_000)
        cached = rnd.randrange(0, 400_000)
        cost = (fresh * rates[0] + completion * rates[1] + cached * rates[2]) / 1e6
        if index in doubles:
            cost *= 2.0
        if deltas and index in deltas:
            cost *= deltas[index]
        rows.append(
            {
                "id": "row-%d" % index,
                "date": "2026-10-10",
                "billing_type": billing_type,
                "model": model,
                "raw_model": model,
                "fresh_input_tokens": fresh,
                "completion_tokens": completion,
                "cached_tokens": cached,
                "total_tokens": fresh + completion + cached,
                "cost_usd": cost,
            }
        )
    return rows


class CreditsFitTests(unittest.TestCase):
    def test_clean_rows_are_accepted_by_both_profiles(self):
        rows = usage_rows()
        for profile in (STRICT, CREDITS):
            fit = monitor.fit_rates(rows, profile, *WINDOW)
            self.assertEqual(monitor.FIT_ACCEPTED, fit["status"], fit["reason"])
            self.assertAlmostEqual(TRUE_RATES[0], fit["rates"]["input"], places=3)

    def test_a_pair_of_outlier_rows_sinks_the_rms_but_not_the_price(self):
        rows = usage_rows(deltas={3: 1.25, 7: 0.75})
        strict = monitor.fit_rates(rows, STRICT, *WINDOW)
        credits = monitor.fit_rates(rows, CREDITS, *WINDOW)
        self.assertEqual(monitor.FIT_REJECTED, strict["status"])
        self.assertIn("RMS residual", strict["reason"])
        self.assertEqual(monitor.FIT_ACCEPTED, credits["status"], credits["reason"])
        self.assertLess(credits["median_relative_error"], 0.05)
        for index, rate_class in enumerate(monitor.FIT_CLASSES):
            self.assertAlmostEqual(
                TRUE_RATES[index], credits["rates"][rate_class], delta=TRUE_RATES[index] * 0.25
            )

    def test_a_wide_relative_error_is_still_rejected(self):
        deltas = {}
        for index in range(30):
            deltas[index] = 1.12 if index % 2 == 0 else 0.88
        # Reconciliation is widened so the median gate is what rejects here.
        profile = dict(CREDITS, max_total_reconciliation_error=0.05)
        fit = monitor.fit_rates(usage_rows(deltas=deltas), profile, *WINDOW)
        self.assertEqual(monitor.FIT_REJECTED, fit["status"])
        self.assertIn("median per-row relative error", fit["reason"])

    def test_two_price_levels_in_one_window_are_rejected(self):
        # Half the rows billed at double the price: never average two bases.
        fit = monitor.fit_rates(usage_rows(doubles=set(range(15, 30))), CREDITS, *WINDOW)
        self.assertEqual(monitor.FIT_REJECTED, fit["status"])
        self.assertIn("reconciliation error", fit["reason"])

    def test_the_credits_profile_still_requires_enough_rows(self):
        fit = monitor.fit_rates(usage_rows(count=5), CREDITS, *WINDOW)
        self.assertEqual(monitor.FIT_REJECTED, fit["status"])
        self.assertIn("minimum is 12", fit["reason"])

    def test_disabling_the_rms_gate_still_records_the_value(self):
        fit = monitor.fit_rates(usage_rows(deltas={3: 1.25}), CREDITS, *WINDOW)
        self.assertIsNone(fit["allowed_rms_residual_usd"])
        self.assertGreater(fit["rms_residual_usd"], 0)
        self.assertIsNotNone(fit["median_relative_error"])
        self.assertIsNotNone(fit["p90_relative_error"])


class BillingPathSelectionTests(unittest.TestCase):
    def setUp(self):
        self.entries = monitor.parse_recommended_models(
            {"clinePass": [{"id": "cline-pass/x"}], "free": []}
        )
        self.entry = self.entries[0]

    def test_the_default_selection_is_the_subscription_rows(self):
        rows = usage_rows(billing_type="cline-pass") + usage_rows(billing_type="cline-usage")
        selected = monitor.rows_for_model(rows, self.entry)
        self.assertTrue(selected)
        self.assertTrue(all(row["billing_type"] == "cline-pass" for row in selected))

    def test_credit_rows_are_selected_only_when_asked_for(self):
        rows = usage_rows(billing_type="cline-pass") + usage_rows(billing_type="cline-usage")
        selected = monitor.rows_for_model(
            rows, self.entry, billing_type=monitor.BILLING_TYPE_CLINE_USAGE
        )
        self.assertEqual(30, len(selected))
        self.assertTrue(all(row["billing_type"] == "cline-usage" for row in selected))


class ResolveRatesTests(unittest.TestCase):
    def setUp(self):
        self.entry = monitor.parse_recommended_models(
            {"clinePass": [{"id": "cline-pass/x"}], "free": []}
        )[0]

    def test_a_credits_fit_is_labelled_and_kept(self):
        fit = monitor.fit_rates(usage_rows(), CREDITS, *WINDOW)
        record = monitor.resolve_rates(
            self.entry,
            fit,
            measured_provenance=monitor.PROVENANCE_MEASURED_CREDITS,
            credits_fit=fit,
        )
        self.assertEqual(
            monitor.PROVENANCE_MEASURED_CREDITS, record["input"]["provenance"]
        )
        self.assertEqual(monitor.PROVENANCE_MEASURED_CREDITS, record["output"]["provenance"])
        self.assertIn("credits_fit", record)
        self.assertEqual(
            monitor.PROVENANCE_INPUT_FALLBACK, record["cache_write"]["provenance"]
        )

    def test_the_default_provenance_stays_plain_measured(self):
        fit = monitor.fit_rates(usage_rows(billing_type="cline-pass"), STRICT, *WINDOW)
        record = monitor.resolve_rates(self.entry, fit)
        self.assertEqual(monitor.PROVENANCE_MEASURED, record["input"]["provenance"])
        self.assertNotIn("credits_fit", record)


class CascadeTests(unittest.TestCase):
    """resolve_model_rates: subscription first, credits second, reference last."""

    def setUp(self):
        self.config = run.load_config()
        self.config["rate_fit"]["credits"] = dict(CREDITS)
        self.entries = monitor.parse_recommended_models(
            {
                "clinePass": [
                    {"id": "cline-pass/pass"},
                    {"id": "cline-pass/credits"},
                    {"id": "cline-pass/bare"},
                ],
                "free": [],
            }
        )
        self.by_id = {entry["id"]: entry for entry in self.entries}
        self.window = {"start": WINDOW[0], "end": WINDOW[1], "days": 30}

    def run_cascade(self, rows, reference_models=None):
        return run.resolve_model_rates(
            self.entries,
            rows,
            self.config,
            reference_models or {},
            {"source_name": "test"},
            self.window,
        )

    def test_a_subscription_fit_wins_and_no_credits_fit_is_attempted(self):
        rows = usage_rows(billing_type="cline-pass", model="pass") + usage_rows(
            billing_type="cline-usage", model="pass", deltas={3: 1.25}
        )
        rates, _fits, rejected = self.run_cascade(rows)
        rates = rates["cline-pass/pass"]
        self.assertEqual(monitor.PROVENANCE_MEASURED, rates["input"]["provenance"])
        self.assertNotIn("credits_fit", rates)
        self.assertNotIn("cline-pass/pass", rejected)

    def test_credit_rows_are_used_when_the_subscription_fit_finds_nothing(self):
        rates, fits, _rejected = self.run_cascade(
            usage_rows(billing_type="cline-usage", model="credits", deltas={3: 1.25, 7: 0.75})
        )
        record = rates["cline-pass/credits"]
        self.assertEqual(
            monitor.PROVENANCE_MEASURED_CREDITS, record["input"]["provenance"]
        )
        self.assertAlmostEqual(TRUE_RATES[0], record["input"]["usd_per_mtok"], delta=0.02)
        self.assertEqual(monitor.PROVENANCE_MEASURED_CREDITS, record["cache_read"]["provenance"])
        self.assertIn("credits", fits)

    def test_a_model_with_no_rows_falls_back_to_the_reference_table(self):
        rates, _fits, rejected = self.run_cascade(
            usage_rows(billing_type="cline-pass", model="pass"),
            {"bare": {"input": 1.0, "output": 2.0, "cache_read": 0.1}},
        )
        record = rates["cline-pass/bare"]
        self.assertEqual(monitor.PROVENANCE_REFERENCE, record["input"]["provenance"])
        self.assertEqual(1.0, record["input"]["usd_per_mtok"])
        self.assertIn("0 usable ClinePass rows", rejected["cline-pass/bare"])

    def test_a_rejected_credits_fit_is_reported_next_to_the_rejection(self):
        rows = usage_rows(
            billing_type="cline-usage", model="credits", doubles=set(range(15, 30))
        )
        _rates, _fits, rejected = self.run_cascade(rows)
        self.assertIn("credits:", rejected["cline-pass/credits"])


class ReferenceDriftTests(unittest.TestCase):
    def fit(self, rates):
        return {"status": monitor.FIT_ACCEPTED, "rates": dict(rates)}

    def test_a_halved_price_is_flagged(self):
        fits = {"m": self.fit({"input": 0.15, "output": 0.60, "cache_read": 0.003})}
        reference = {"m": {"input": 0.30, "output": 1.20, "cache_read": 0.006}}
        warning = monitor.reference_drift_warning(fits, reference, 1.5)
        self.assertIsNotNone(warning)
        self.assertIn("m (0.50x", warning)
        self.assertIn("1.5x", warning)

    def test_a_small_difference_is_not_flagged(self):
        fits = {"m": self.fit({"input": 1.1, "output": 1.1, "cache_read": 1.1})}
        reference = {"m": {"input": 1.0, "output": 1.0, "cache_read": 1.0}}
        self.assertIsNone(monitor.reference_drift_warning(fits, reference, 1.5))

    def test_the_threshold_is_configurable(self):
        fits = {"m": self.fit({"input": 1.2, "output": 1.2, "cache_read": 1.2})}
        reference = {"m": {"input": 1.0, "output": 1.0, "cache_read": 1.0}}
        self.assertIsNone(monitor.reference_drift_warning(fits, reference, 1.5))
        self.assertIsNotNone(monitor.reference_drift_warning(fits, reference, 1.15))

    def test_models_without_a_reference_entry_or_fit_are_skipped(self):
        fits = {"m": self.fit({"input": 0.15, "output": 0.6, "cache_read": 0.003})}
        self.assertIsNone(monitor.reference_drift_warning(fits, {}, 1.5))
        self.assertIsNone(
            monitor.reference_drift_warning(
                {"m": {"status": monitor.FIT_REJECTED, "rates": {}}},
                {"m": {"input": 1.0}},
                1.5,
            )
        )


class SiteLabelsTests(unittest.TestCase):
    def credits_model(self):
        fit = monitor.fit_rates(usage_rows(), CREDITS, *WINDOW)
        entry = monitor.parse_recommended_models(
            {"clinePass": [{"id": "cline-pass/x"}], "free": []}
        )[0]
        rates = monitor.resolve_rates(
            entry, fit, measured_provenance=monitor.PROVENANCE_MEASURED_CREDITS, credits_fit=fit
        )
        return {"id": "cline-pass/x", "name": "X", "rates": rates}

    def test_a_credits_fit_is_labelled_measured_credits(self):
        label, _css = site.model_rate_source(self.credits_model())
        self.assertEqual("measured (credits)", label)

    def test_a_model_with_no_rows_says_so_instead_of_unavailable(self):
        entry = monitor.parse_recommended_models(
            {"clinePass": [{"id": "cline-pass/x"}], "free": []}
        )[0]
        empty = monitor.fit_rates([], STRICT, *WINDOW)
        rates = monitor.resolve_rates(entry, empty)
        model = {"id": "cline-pass/x", "name": "X", "rates": rates}
        label, _css = site.model_rate_source(model)
        self.assertEqual("no billing rows yet", label)
        self.assertIn("no rows yet", site._rate_cell(model, "input", no_rows=True))

    def test_a_model_with_unusable_rows_still_says_unavailable(self):
        entry = monitor.parse_recommended_models(
            {"clinePass": [{"id": "cline-pass/x"}], "free": []}
        )[0]
        fit = monitor.fit_rates(usage_rows(count=3), STRICT, *WINDOW)
        rates = monitor.resolve_rates(entry, fit)
        model = {"id": "cline-pass/x", "name": "X", "rates": rates}
        label, _css = site.model_rate_source(model)
        self.assertEqual("unavailable", label)

    def test_the_stats_count_credits_rates_separately(self):
        entry = self.credits_model()
        model = {
            "id": entry["id"],
            "slug": "x",
            "name": "X",
            "free": False,
            "priced": True,
            "rates": entry["rates"],
            "quality": {"intelligence_index": 40.0},
            "cost_usd": {"planning": 0.01},
            "efficiency": {"planning": None},
            "pareto": {"planning": False},
        }
        scored = monitor._with_chart(
            [model], {"planning": {}}, {"planning": 0.01}, {"planning": None}
        )
        self.assertEqual(1, scored["stats"]["measured_rates"])
        self.assertEqual(1, scored["stats"]["measured_credits_rates"])


REPRICED_OLD = TRUE_RATES
REPRICED_NEW = (0.15, 0.96, 0.003)


def repriced_rows(old=REPRICED_OLD, new=REPRICED_NEW, count=20):
    """Twenty rows at the old price and twenty at the new one, split on 20 Oct."""
    older = usage_rows(count=count, rates=old, seed=11, model="x")
    newer = usage_rows(count=count, rates=new, seed=12, model="x")
    for index, row in enumerate(older):
        row["date"] = "2026-10-%02d" % (1 + index % 19)
    for index, row in enumerate(newer):
        row["date"] = "2026-10-%02d" % (20 + index % 11)
    return older + newer


class ChangePointTests(unittest.TestCase):
    """A model repriced inside the window keeps a price for the new regime."""

    def test_the_whole_window_rejects_two_price_levels(self):
        fit = monitor.fit_rates(repriced_rows(), CREDITS, *WINDOW)
        self.assertEqual(monitor.FIT_REJECTED, fit["status"])

    def test_the_newer_regime_is_published_instead_of_no_price(self):
        fit = monitor.fit_rates_with_change_point(
            repriced_rows(), CREDITS, *WINDOW, change_config={"enabled": True}
        )
        self.assertEqual(monitor.FIT_ACCEPTED, fit["status"], fit["reason"])
        change = fit["change_point"]
        self.assertTrue(change["detected"])
        self.assertEqual("2026-10-20", change["date"])
        self.assertEqual(change["date"], fit["window_start"])
        self.assertEqual(WINDOW[0], fit["window_requested_start"])
        self.assertAlmostEqual(REPRICED_NEW[1], fit["rates"]["output"], delta=REPRICED_NEW[1] * 0.05)
        self.assertAlmostEqual(REPRICED_OLD[1], change["rates_before"]["output"], delta=REPRICED_OLD[1] * 0.05)
        self.assertAlmostEqual(0.6, change["relative_shift"]["output"], delta=0.05)
        self.assertEqual(20, change["rows_after"])

    def test_a_single_price_level_is_not_split(self):
        rows = usage_rows(count=40, seed=5)
        for index, row in enumerate(rows):
            row["date"] = "2026-10-%02d" % (1 + index % 28)
        fit = monitor.fit_rates_with_change_point(
            rows, CREDITS, *WINDOW, change_config={"enabled": True}
        )
        self.assertEqual(monitor.FIT_ACCEPTED, fit["status"])
        self.assertIsNone(fit["change_point"])
        self.assertEqual(WINDOW[0], fit["window_start"])

    def test_a_shift_below_the_threshold_is_not_published_as_a_repricing(self):
        # 16% across the board: too wide for one level (the median error gate
        # rejects it) but below the 20% the detector will call a repricing.
        modest = tuple(rate * 1.16 for rate in REPRICED_OLD)
        fit = monitor.fit_rates_with_change_point(
            repriced_rows(new=modest), CREDITS, *WINDOW, change_config={"enabled": True}
        )
        self.assertEqual(monitor.FIT_REJECTED, fit["status"])
        self.assertIn("no split of the window", fit["reason"])

    def test_noise_is_still_rejected_and_the_split_is_recorded_as_attempted(self):
        deltas = {index: 1.12 if index % 2 == 0 else 0.88 for index in range(40)}
        rows = usage_rows(count=40, deltas=deltas, seed=9)
        for index, row in enumerate(rows):
            row["date"] = "2026-10-%02d" % (1 + index % 28)
        profile = dict(CREDITS, max_total_reconciliation_error=0.05)
        fit = monitor.fit_rates_with_change_point(
            rows, profile, *WINDOW, change_config={"enabled": True}
        )
        self.assertEqual(monitor.FIT_REJECTED, fit["status"])
        self.assertIn("no split of the window", fit["reason"])

    def test_disabling_the_search_keeps_the_plain_rejection(self):
        fit = monitor.fit_rates_with_change_point(
            repriced_rows(), CREDITS, *WINDOW, change_config={"enabled": False}
        )
        self.assertEqual(monitor.FIT_REJECTED, fit["status"])
        self.assertIsNone(fit["change_point"])

    def test_the_cascade_publishes_the_repriced_credits_rate(self):
        config = run.load_config()
        entry = monitor.parse_recommended_models(
            {"clinePass": [{"id": "cline-pass/x"}], "free": []}
        )[0]
        window = {"start": WINDOW[0], "end": WINDOW[1], "days": 30}
        rates, fits, rejected = run.resolve_model_rates(
            [entry], repriced_rows(), config, {}, {"source_name": "test"}, window
        )
        record = rates["cline-pass/x"]
        self.assertNotIn("cline-pass/x", rejected)
        self.assertEqual(monitor.PROVENANCE_MEASURED_CREDITS, record["output"]["provenance"])
        self.assertAlmostEqual(REPRICED_NEW[1], record["output"]["usd_per_mtok"], delta=REPRICED_NEW[1] * 0.05)
        self.assertEqual("2026-10-20", record["output"]["window_start"])
        self.assertIn("x", fits)


class RepricedModelLabelTests(unittest.TestCase):
    def model(self, fit, credits_fit=None):
        entry = monitor.parse_recommended_models(
            {"clinePass": [{"id": "cline-pass/x"}], "free": []}
        )[0]
        rates = monitor.resolve_rates(
            entry,
            fit,
            measured_provenance=monitor.PROVENANCE_MEASURED_CREDITS
            if credits_fit
            else monitor.PROVENANCE_MEASURED,
            credits_fit=credits_fit,
        )
        return {"id": "cline-pass/x", "name": "X", "rates": rates}

    def repriced_fit(self):
        return monitor.fit_rates_with_change_point(
            repriced_rows(), CREDITS, *WINDOW, change_config={"enabled": True}
        )

    def test_the_label_names_the_effective_start_of_the_published_regime(self):
        fit = self.repriced_fit()
        label, _css = site.model_rate_source(self.model(fit, credits_fit=fit))
        self.assertEqual("measured (credits, from 2026-10-20)", label)

    def test_credit_billed_rows_are_not_reported_as_no_rows(self):
        # A rejected credits fit that still saw thousands of rows is "unavailable",
        # not "no billing rows yet": the reader must be able to tell a missing
        # model from a fit that was thrown away.
        rejected = monitor.fit_rates(
            usage_rows(deltas={index: 1.5 for index in range(0, 30, 3)}), CREDITS, *WINDOW
        )
        self.assertEqual(monitor.FIT_REJECTED, rejected["status"])
        model = self.model(rejected, credits_fit=rejected)
        label, _css = site.model_rate_source(model)
        self.assertEqual("unavailable", label)
        self.assertNotIn("no rows yet", site._rate_cell(model, "input", no_rows=False))


if __name__ == "__main__":
    unittest.main()
