"""The ClinePass reference-rate refresh (scripts/refresh_reference_rates.py).

The reference table is the only fallback price for a model whose own usage fit
is not estimable, so a stale table silently removes models from the
cost/quality chart. These tests pin the parsing rules that keep it trustworthy.
"""

import json
import unittest
from pathlib import Path

from tests.support import ROOT

from scripts import refresh_reference_rates as rates

FIXTURE = ROOT / "tests" / "fixtures" / "clinepass_pricing.md"


class ParsingTests(unittest.TestCase):
    def setUp(self):
        self.entries = rates.build_entries(rates.parse_rows(FIXTURE.read_text(encoding="utf-8")))

    def test_variant_rows_collapse_onto_one_slug_per_model(self):
        self.assertEqual(
            {
                "deepseek-v4.1-flash",
                "glm-5.3",
                "kimi-k3.1-pro",
                "minimax-m3",
                "mimo-v2.6-pro",
                "qwen3.7-plus",
                "qwen3.8-max",
            },
            set(self.entries),
        )

    def test_peak_beats_off_peak(self):
        self.assertEqual(
            {"input": 0.3, "output": 1.2, "cache_read": 0.006, "cache_write": None},
            self.entries["deepseek-v4.1-flash"],
        )

    def test_the_smaller_context_tier_is_kept(self):
        self.assertEqual(0.3, self.entries["minimax-m3"]["input"])
        self.assertEqual(0.4, self.entries["qwen3.7-plus"]["input"])

    def test_cache_write_takes_the_first_documented_tier(self):
        self.assertEqual(2.0, self.entries["kimi-k3.1-pro"]["cache_write"])

    def test_a_row_without_a_variant_beats_a_tiered_one(self):
        entry = rates.build_entries(
            [
                ("GLM 5.3 (Peak)", 1.0, 2.0, 0.1, None),
                ("GLM 5.3", 1.4, 4.4, 0.26, None),
            ]
        )["glm-5.3"]
        self.assertEqual(1.4, entry["input"])

    def test_the_table_header_and_other_sections_are_ignored(self):
        self.assertNotIn("model", self.entries)
        self.assertNotIn("not-part-of-the-table", self.entries)

    def test_a_page_without_the_table_fails_loudly(self):
        with self.assertRaises(ValueError):
            rates.parse_rows("# ClinePass\n\nNo prices here.\n")


class DriftTests(unittest.TestCase):
    def setUp(self):
        self.entries = rates.build_entries(rates.parse_rows(FIXTURE.read_text(encoding="utf-8")))

    def test_a_missing_model_is_reported_as_new(self):
        stored = {"models": {"glm-5.3": self.entries["glm-5.3"]}}
        added, changed, gone = rates.diff(stored, self.entries)
        self.assertIn("kimi-k3.1-pro", added)
        self.assertEqual([], changed)
        self.assertEqual([], gone)

    def test_a_moved_price_is_reported_as_drift(self):
        stored = {
            "models": {
                "qwen3.8-max": {
                    "input": 2.0,
                    "output": 6.0,
                    "cache_read": 0.25,
                    "cache_write": 2.5,
                }
            }
        }
        _added, changed, _gone = rates.diff(stored, self.entries)
        self.assertEqual(["qwen3.8-max.cache_read: 0.25 -> 0.17"], changed)

    def test_a_delisted_model_is_kept_not_deleted(self):
        stored = {"models": {"legacy-model": {"input": 1.0, "output": 2.0}}}
        _added, _changed, gone = rates.diff(stored, self.entries)
        self.assertIn("legacy-model", gone)


class SerializationTests(unittest.TestCase):
    def test_dump_round_trips_and_keeps_one_line_per_model(self):
        document = {
            "version": 1,
            "retrieved_at": "2026-11-06",
            "models": {
                "glm-5.3": {"input": 1.4, "output": 4.4, "cache_read": 0.26, "cache_write": None},
                "deepseek-v4-pro": {
                    "input": 1.32,
                    "output": 3.96,
                    "cache_read": 0.044,
                    "cache_write": None,
                    "off_peak": {"input": 0.66, "output": 1.98, "cache_read": 0.022},
                    "note": "Peak values are listed.",
                },
            },
            "free_models": {"ids": ["cline-free/x"]},
        }
        text = rates.dump_document(document)
        self.assertEqual(document, json.loads(text))
        lines = [line for line in text.splitlines() if line.strip().startswith('"glm-5.3"')]
        self.assertEqual(1, len(lines))
        self.assertIn('"input": 1.4', lines[0])

    def test_apply_refreshes_prices_and_keeps_hand_written_notes(self):
        stored = {
            "models": {
                "deepseek-v4-pro": {
                    "input": 9.9,
                    "output": 9.9,
                    "cache_read": None,
                    "cache_write": None,
                    "note": "Peak values are listed.",
                    "off_peak": {"input": 0.66, "output": 1.98, "cache_read": 0.022},
                }
            }
        }
        entry = {"input": 1.32, "output": 3.96, "cache_read": 0.044, "cache_write": None}
        updated = rates.apply(stored, {"deepseek-v4-pro": entry}, "2026-11-06")
        model = updated["models"]["deepseek-v4-pro"]
        self.assertEqual(1.32, model["input"])
        self.assertEqual("Peak values are listed.", model["note"])
        self.assertEqual(0.66, model["off_peak"]["input"])
        self.assertEqual("2026-11-06", updated["retrieved_at"])


if __name__ == "__main__":
    unittest.main()
