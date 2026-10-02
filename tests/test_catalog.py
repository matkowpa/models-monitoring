"""Catalog discovery, mapping, and quality ingestion (section 7 step 1 and 4)."""

import unittest

from tests.support import config, load
from src import monitor, run


class RecommendedModelsTests(unittest.TestCase):
    def setUp(self):
        self.entries = monitor.parse_recommended_models(load("recommended_models.json"))

    def test_paid_and_free_entries_are_separated_by_array_membership(self):
        paid = [entry for entry in self.entries if not entry["free"]]
        free = [entry for entry in self.entries if entry["free"]]
        self.assertEqual(12, len(paid))
        self.assertEqual(3, len(free))
        self.assertTrue(all(entry["id"].startswith("cline-pass/") for entry in paid))

    def test_provider_style_free_id_is_kept_not_filtered_by_prefix(self):
        ids = {entry["id"] for entry in self.entries}
        self.assertIn("stealth/space-bunny-alpha", ids)
        stealth = next(entry for entry in self.entries if entry["id"].startswith("stealth/"))
        self.assertTrue(stealth["free"])
        self.assertEqual("stealth/space-bunny-alpha", stealth["slug"])

    def test_slugs_drop_the_provider_prefix(self):
        slugs = {entry["slug"] for entry in self.entries}
        self.assertIn("glm-5.3", slugs)
        self.assertIn("gemini-3.8-flash", slugs)

    def test_the_payload_carries_no_prices(self):
        for entry in self.entries:
            self.assertNotIn("pricing", entry)
            self.assertNotIn("input", entry)

    def test_duplicates_and_string_entries_are_tolerated(self):
        entries = monitor.parse_recommended_models(
            {"clinePass": ["cline-pass/glm-5.3", {"id": "cline-pass/glm-5.3"}, {"id": ""}]}
        )
        self.assertEqual(1, len(entries))
        self.assertEqual("cline-pass/glm-5.3", entries[0]["name"])

    def test_missing_or_invalid_arrays(self):
        self.assertEqual([], monitor.parse_recommended_models({"clinePass": []}))
        self.assertFalse(monitor.has_paid_models(monitor.parse_recommended_models({})))
        with self.assertRaises(monitor.ApiError):
            monitor.parse_recommended_models({"clinePass": "nope"})
        with self.assertRaises(monitor.ApiError):
            monitor.parse_recommended_models([1, 2, 3])

    def test_pinned_fallback_catalog_is_usable_and_flags_free_entries(self):
        entries = monitor.parse_catalog_fallback(load("clinepass_catalog_fallback.json"))
        self.assertTrue(monitor.has_paid_models(entries))
        self.assertTrue(any(entry["free"] for entry in entries))
        with self.assertRaises(monitor.ApiError):
            monitor.parse_catalog_fallback({"models": "nope"})


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.mapping = load("model_mapping.json")["models"]
        aa = config()["artificial_analysis"]
        self.aa_index = monitor.parse_aa_models(
            load("aa_models.json"),
            intelligence_key=aa["intelligence_index_key"],
            coding_key=aa["coding_index_key"],
            default_scale=aa["index_scale"],
        )
        self.entries = monitor.parse_recommended_models(load("recommended_models.json"))

    def test_mapping_resolves_every_expected_paid_model(self):
        matched = sum(
            1
            for entry in self.entries
            if monitor.match_aa_record(entry, self.aa_index, self.mapping)[0]
        )
        self.assertEqual(11, matched)

    def test_unmatched_model_yields_no_score_instead_of_an_estimate(self):
        entry = next(
            item for item in self.entries if item["id"].endswith("muse-spark-1.3-contributor")
        )
        record, _key = monitor.match_aa_record(entry, self.aa_index, self.mapping)
        self.assertIsNone(record)
        self.assertIsNone(monitor.quality_record(record, "2026-10-02T06:00:00+02:00"))

    def test_ambiguous_aa_key_is_left_unmatched(self):
        payload = [
            {"name": "GLM-5.3", "slug": "glm-5-3-a", "evaluations": {"i": 70}},
            {"name": "GLM-5.3", "slug": "glm-5-3-b", "evaluations": {"i": 60}},
        ]
        index = monitor.parse_aa_models(payload, intelligence_key="i")
        entry = {"id": "cline-pass/glm-5.3", "slug": "glm-5.3", "name": "GLM-5.3"}
        record, _key = monitor.match_aa_record(entry, index, {})
        self.assertIsNone(record)

    def test_quality_record_marks_the_coding_index_as_display_only(self):
        entry = next(item for item in self.entries if item["slug"] == "glm-5.3")
        record, key = monitor.match_aa_record(entry, self.aa_index, self.mapping)
        quality = monitor.quality_record(record, "2026-10-02T06:00:00+02:00")
        self.assertEqual(71.0, quality["intelligence_index"])
        self.assertEqual(66.0, quality["coding_index"])
        self.assertTrue(quality["coding_index_display_only"])
        self.assertEqual("glm-5-3", key)
        self.assertEqual("2026-10-02T06:00:00+02:00", quality["retrieved_at"])

    def test_missing_index_clears_any_previous_score(self):
        record = {"name": "X", "aa_slug": "x", "intelligence_index": None, "coding_index": 50.0}
        self.assertIsNone(monitor.quality_record(record, None))

    def test_declared_scale_is_normalized_onto_zero_to_hundred(self):
        self.assertEqual(50.0, monitor.normalize_quality(5, {"min": 0, "max": 10}))
        self.assertEqual(100.0, monitor.normalize_quality(150, {"min": 0, "max": 100}))
        self.assertIsNone(monitor.normalize_quality(None))
        self.assertIsNone(monitor.normalize_quality(5, {"min": 4, "max": 4}))


class EnvelopeAndAccountTests(unittest.TestCase):
    def test_failure_envelope_raises_instead_of_returning_zero_rows(self):
        with self.assertRaises(monitor.ApiError):
            monitor.unwrap_envelope(load("malformed_envelope.json"))
        with self.assertRaises(monitor.ApiError):
            monitor.parse_usage_page(load("malformed_envelope.json"))

    def test_missing_data_field_raises(self):
        with self.assertRaises(monitor.ApiError):
            monitor.unwrap_envelope({"success": True})

    def test_paging_guard_rejects_a_repeated_cursor(self):
        original = run.http_json
        calls = {"count": 0}

        def fake_http_json(url, **kwargs):
            if url.endswith("/users/me"):
                return {"data": {"id": "usr-1"}, "success": True}
            calls["count"] += 1
            return {
                "data": {"items": [{"id": "u1"}], "nextToken": "same-cursor", "total": 2},
                "success": True,
            }

        import os

        os.environ["CLINE_API_KEY"] = "test-key"
        run.http_json = fake_http_json
        try:
            with self.assertRaises(run.HttpError) as caught:
                run.fetch_usage_rows(config())
        finally:
            run.http_json = original
            os.environ.pop("CLINE_API_KEY", None)
        self.assertIn("repeated pagination cursor", str(caught.exception))
        self.assertEqual(2, calls["count"])

    def test_account_id_mismatch_fails_closed(self):
        original = run.http_json
        import os

        def fake_http_json(url, **kwargs):
            if url.endswith("/users/me"):
                return {"data": {"id": "usr-real"}, "success": True}
            return {"data": {"items": [], "nextToken": None, "total": 0}, "success": True}

        os.environ["CLINE_API_KEY"] = "test-key"
        os.environ["CLINE_USER_ID"] = "usr-other"
        run.http_json = fake_http_json
        try:
            with self.assertRaises(run.HttpError) as caught:
                run.fetch_usage_rows(config())
        finally:
            run.http_json = original
            os.environ.pop("CLINE_API_KEY", None)
            os.environ.pop("CLINE_USER_ID", None)
        self.assertIn("does not match", str(caught.exception))

    def test_live_run_without_a_cline_key_fails_closed(self):
        import os

        os.environ.pop("CLINE_API_KEY", None)
        with self.assertRaises(run.HttpError):
            run.fetch_usage_rows(config())

    def test_live_run_without_an_aa_key_fails_closed(self):
        import os

        os.environ.pop("ARTIFICIAL_ANALYSIS_API_KEY", None)
        with self.assertRaises(run.HttpError):
            run.fetch_aa_models(config())


if __name__ == "__main__":
    unittest.main()
