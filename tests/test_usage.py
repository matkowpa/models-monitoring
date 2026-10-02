"""Billing usage ingestion (section 7 step 2)."""

import unittest
from datetime import date

from tests.support import (
    FIXTURES,
    config,
    fixture_rows,
    ground_truth,
    load,
    normalized_fixture_rows,
)
from src import monitor, run


class UsageParsingTests(unittest.TestCase):
    def test_usage_page_shape(self):
        items, token, total = monitor.parse_usage_page(load("usage_page_1.json"))
        self.assertTrue(items)
        self.assertEqual("cursor-page-2", token)
        self.assertEqual(563, total)
        self.assertIsNone(monitor.parse_usage_page(load("usage_page_2.json"))[1])

    def test_both_fixture_pages_load_into_one_row_set(self):
        self.assertEqual(563, len(fixture_rows()))

    def test_money_scales_are_applied_and_raw_integers_kept(self):
        scales = config()["money_scales"]
        row = monitor.normalize_usage_row(
            {
                "id": "u1",
                "createdAt": "2026-10-02T06:00:00Z",
                "costUsd": 55770,
                "creditsUsed": 2500000,
                "aiModelTypeName": "cline-pass",
                "aiModelName": "glm-5.3",
                "promptTokens": 96000,
                "completionTokens": 4200,
                "cachedTokens": 90000,
            },
            scales,
        )
        self.assertAlmostEqual(55770 / 100000000, row["cost_usd"], places=12)
        self.assertAlmostEqual(2.5, row["credits_usd"], places=12)
        self.assertEqual(55770, row["cost_usd_raw"])
        self.assertEqual(2500000, row["credits_usd_raw"])
        self.assertEqual(6000, row["fresh_input_tokens"])
        self.assertEqual("cline-pass", row["billing_type"])

    def test_inconsistent_rows_are_dropped_and_counted(self):
        rows = [
            {"promptTokens": 100, "completionTokens": 10, "cachedTokens": 5000},
            {"promptTokens": 100, "completionTokens": 10, "cachedTokens": -5},
            {"promptTokens": 100, "completionTokens": 10, "cachedTokens": 40},
        ]
        normalized, dropped = monitor.normalize_usage_rows(rows, config()["money_scales"])
        self.assertEqual(1, len(normalized))
        self.assertEqual(2, dropped)
        self.assertEqual(60, normalized[0]["fresh_input_tokens"])

    def test_total_tokens_fall_back_to_prompt_plus_completion(self):
        row = monitor.normalize_usage_row(
            {"promptTokens": 100, "completionTokens": 25}, config()["money_scales"]
        )
        self.assertEqual(125, row["total_tokens"])


class RowSelectionTests(unittest.TestCase):
    def setUp(self):
        self.rows, self.dropped = normalized_fixture_rows()
        self.entries = monitor.parse_recommended_models(load("recommended_models.json"))

    def entry(self, slug):
        return next(item for item in self.entries if item["slug"] == slug)

    def test_only_clinepass_rows_of_that_model_are_selected(self):
        selected = monitor.rows_for_model(self.rows, self.entry("glm-5.3"))
        self.assertEqual(80, len(selected))
        self.assertTrue(all(row["billing_type"] == "cline-pass" for row in selected))
        self.assertTrue(all(row["model"] == "glm-5.3" for row in selected))

    def test_usage_billing_rows_are_excluded_from_the_fit(self):
        selected = monitor.rows_for_model(self.rows, self.entry("glm-5.3"))
        self.assertFalse(any(row["model"] == "glm-5.3" and row["billing_type"] != "cline-pass" for row in selected))

    def test_free_twin_rows_do_not_contaminate_the_paid_model(self):
        paid = monitor.rows_for_model(self.rows, self.entry("deepseek-v4.1-flash"))
        self.assertTrue(all(row["model"] == "deepseek-v4.1-flash" for row in paid))

    def test_dropped_row_count_is_reported(self):
        self.assertEqual(1, self.dropped)

    def test_observed_blended_rate_matches_the_billed_rows(self):
        # kimi-k3 has no off-window rows in the fixture, so the observed blended
        # rate must equal the token-weighted mean of its ground-truth rates.
        selected = monitor.rows_for_model(self.rows, self.entry("kimi-k3"))
        observed, counted = monitor.observed_blended_rate(selected)
        rates = ground_truth()["kimi-k3"]
        tokens = sum(row["total_tokens"] for row in selected)
        expected = sum(
            row["fresh_input_tokens"] * rates[0]
            + row["completion_tokens"] * rates[1]
            + row["cached_tokens"] * rates[2]
            for row in selected
        ) / tokens
        self.assertEqual(60, counted)
        self.assertAlmostEqual(expected, observed, places=6)


class DailyWindowTests(unittest.TestCase):
    def test_range_is_split_into_windows_of_at_most_31_days(self):
        windows = monitor.daily_windows("2026-01-01", "2026-03-15", max_days=31)
        self.assertEqual(3, len(windows))
        self.assertEqual(("2026-01-01", "2026-01-31"), windows[0])
        self.assertEqual(("2026-02-01", "2026-03-03"), windows[1])
        self.assertEqual(("2026-03-04", "2026-03-15"), windows[2])
        for start, end in windows:
            span = (date.fromisoformat(end) - date.fromisoformat(start)).days
            self.assertLessEqual(span, 30)

    def test_single_day_and_boundary_cases(self):
        self.assertEqual([("2026-01-01", "2026-01-01")], monitor.daily_windows("2026-01-01", "2026-01-01"))
        self.assertEqual([], monitor.daily_windows("2026-02-01", "2026-01-01"))
        self.assertEqual(
            [("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-01")],
            monitor.daily_windows("2026-01-01", "2026-02-01"),
        )

    def test_fixture_date_anchors_are_recent_relative_to_today(self):
        rows, _dropped = normalized_fixture_rows()
        newest = max(row["date"] for row in rows)
        self.assertEqual(date.today().isoformat(), newest)


if __name__ == "__main__":
    unittest.main()
