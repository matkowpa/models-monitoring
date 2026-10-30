"""Quality provenance: identity matching, carry-forward, and index revisions."""

import unittest

from tests.support import config, load
from src import monitor, run, site


def aa_payload(*models):
    payload = []
    for name, slug, intelligence, coding in models:
        payload.append(
            {
                "name": name,
                "slug": slug,
                "evaluations": {
                    "i": intelligence,
                    "c": coding,
                },
            }
        )
    return payload


def entry(slug, name=None, free=False):
    return {
        "id": "cline-pass/" + slug,
        "slug": slug,
        "name": name or slug,
        "free": free,
    }


class IdentityKeyTests(unittest.TestCase):
    def test_date_code_variant_and_vendor_are_folded_away(self):
        self.assertEqual(
            monitor.identity_key("deepseek-v4.1-flash-1017"),
            monitor.identity_key("DeepSeek V4.1 Flash 1017 (Max)"),
        )
        self.assertEqual(
            monitor.identity_key("MoonshotAI: Kimi K3.1 Pro"),
            monitor.identity_key("kimi-k3.1-pro"),
        )

    def test_checkpoint_digits_are_not_a_version(self):
        self.assertNotEqual(
            monitor.identity_key("qwen3.7-max"), monitor.identity_key("qwen3.8-max")
        )
        self.assertNotEqual(
            monitor.identity_key("glm-5.3"), monitor.identity_key("glm-5.3-flash")
        )

    def test_bare_tier_word_is_kept(self):
        # "Max" outside brackets is a product tier, not an effort level.
        self.assertNotEqual(
            monitor.identity_key("qwen3.7-max"), monitor.identity_key("qwen3.7")
        )


class IdentityMatchTests(unittest.TestCase):
    def setUp(self):
        self.mapping = (load("model_mapping.json") or {}).get("models") or {}

    def test_dated_checkpoint_scores_the_undated_catalog_entry(self):
        index = monitor.parse_aa_models(
            aa_payload(("DeepSeek V4.1 Flash 1017 (Max)", "deepseek-v4-1-flash-1017", 45.1, 60.0)),
            intelligence_key="i",
            coding_key="c",
        )
        record, _key = monitor.match_aa_record(
            entry("deepseek-v4.1-flash"), index, self.mapping
        )
        self.assertIsNotNone(record)
        self.assertEqual(45.1, record["intelligence_index"])

    def test_newest_checkpoint_wins_when_both_exist(self):
        index = monitor.parse_aa_models(
            aa_payload(
                ("DeepSeek V4.1 Flash 0813 (Max)", "deepseek-v4-1-flash-0813", 40.0, 55.0),
                ("DeepSeek V4.1 Flash 1017 (Max)", "deepseek-v4-1-flash-1017", 45.0, 60.0),
            ),
            intelligence_key="i",
            coding_key="c",
        )
        # No explicit pin: both checkpoints are candidates for the undated entry.
        record, _key = monitor.match_aa_record(entry("deepseek-v4.1-flash"), index, {})
        self.assertEqual("DeepSeek V4.1 Flash 1017 (Max)", record["name"])

    def test_pinned_checkpoint_is_preferred_while_it_is_published(self):
        index = monitor.parse_aa_models(
            aa_payload(
                ("DeepSeek V4.1 Flash 0813 (Max)", "deepseek-v4-1-flash-0813", 40.0, 55.0),
                ("DeepSeek V4.1 Flash 1017 (Max)", "deepseek-v4-1-flash-1017", 45.0, 60.0),
            ),
            intelligence_key="i",
            coding_key="c",
        )
        mapping = {"deepseek-v4.1-flash": {"aa_slug": "deepseek-v4-1-flash-0813"}}
        evidence = monitor.match_aa_evidence(entry("deepseek-v4.1-flash"), index, mapping)
        self.assertEqual("exact", evidence["via"])
        self.assertEqual("DeepSeek V4.1 Flash 0813 (Max)", evidence["record"]["name"])

    def test_vanished_pinned_checkpoint_falls_through_to_the_successor(self):
        index = monitor.parse_aa_models(
            aa_payload(("DeepSeek V4.1 Flash 1017 (Max)", "deepseek-v4-1-flash-1017", 45.0, 60.0)),
            intelligence_key="i",
            coding_key="c",
        )
        mapping = {"deepseek-v4.1-flash": {"aa_slug": "deepseek-v4-1-flash-0813"}}
        evidence = monitor.match_aa_evidence(entry("deepseek-v4.1-flash"), index, mapping)
        self.assertEqual("identity", evidence["via"])
        self.assertEqual("DeepSeek V4.1 Flash 1017 (Max)", evidence["record"]["name"])

    def test_evidence_names_the_competing_records(self):
        index = monitor.parse_aa_models(
            aa_payload(
                ("DeepSeek V4.1 Flash 0813 (Max)", "deepseek-v4-1-flash-0813", 40.0, 55.0),
                ("DeepSeek V4.1 Flash 1017 (Max)", "deepseek-v4-1-flash-1017", 45.0, 60.0),
            ),
            intelligence_key="i",
            coding_key="c",
        )
        evidence = monitor.match_aa_evidence(entry("deepseek-v4.1-flash"), index, self.mapping)
        self.assertEqual("identity", evidence["via"])
        self.assertEqual(2, len(evidence["candidates"]))

    def test_exact_match_is_preferred_and_labelled(self):
        index = monitor.parse_aa_models(
            aa_payload(("GLM-5.3", "glm-5-3", 44.8, 74.8)), intelligence_key="i", coding_key="c"
        )
        evidence = monitor.match_aa_evidence(entry("glm-5.3"), index, self.mapping)
        self.assertEqual("exact", evidence["via"])


class CarryForwardTests(unittest.TestCase):
    def test_carried_score_is_kept_and_marked_stale(self):
        prior = {
            "intelligence_index": 43.6,
            "coding_index": 61.0,
            "aa_name": "Kimi K3 (Max)",
            "retrieved_at": "2026-10-26T09:00:00+02:00",
            "fresh": True,
        }
        carried = monitor.carried_quality(prior, "2026-10-26")
        self.assertEqual(43.6, carried["intelligence_index"])
        self.assertFalse(carried["fresh"])
        self.assertEqual("2026-10-26", carried["stale_since"])

    def test_collect_quality_reaches_past_the_immediately_previous_snapshot(self):
        entries = [entry("kimi-k3")]
        prior = {
            "generated": "2026-10-26T09:00:00+02:00",
            "models": [
                {
                    "id": "cline-pass/kimi-k3",
                    "quality": {
                        "intelligence_index": 43.6,
                        "retrieved_at": "2026-10-26T09:00:00+02:00",
                    },
                }
            ],
        }
        gap = {"generated": "2026-10-28T09:00:00+02:00", "models": [{"id": "cline-pass/kimi-k3"}]}
        quality, evidence, matched, carried = run.collect_quality(
            entries,
            {},
            {},
            "2026-10-30T09:00:00+02:00",
            [gap, prior],
        )
        self.assertEqual(0, matched)
        self.assertEqual(1, carried)
        self.assertEqual(43.6, quality["cline-pass/kimi-k3"]["intelligence_index"])
        self.assertEqual("carried", evidence["cline-pass/kimi-k3"]["via"])

    def test_carry_is_refused_across_an_index_revision(self):
        entries = [entry("kimi-k3")]
        before = {
            "generated": "2026-10-26T09:00:00+02:00",
            "report": {"date": "2026-10-26"},
            "changes": {},
            "models": [
                {
                    "id": "cline-pass/kimi-k3",
                    "quality": {
                        "intelligence_index": 43.6,
                        "retrieved_at": "2026-10-26T09:00:00+02:00",
                    },
                }
            ],
        }
        revision = {
            "generated": "2026-10-28T09:00:00+02:00",
            "report": {"date": "2026-10-28"},
            "changes": {"quality_epoch": {"detected": True, "compared": 10}},
            "models": [{"id": "cline-pass/kimi-k3"}],
        }
        quality, evidence, _matched, carried = run.collect_quality(
            entries, {}, {}, "2026-10-30T09:00:00+02:00", [revision, before]
        )
        self.assertEqual(0, carried)
        self.assertIsNone(quality["cline-pass/kimi-k3"])
        self.assertEqual("pre-revision", evidence["cline-pass/kimi-k3"]["reason"])
        self.assertEqual(43.6, evidence["cline-pass/kimi-k3"]["last_value"])
        self.assertEqual("2026-10-28", evidence["cline-pass/kimi-k3"]["revision_day"])

    def test_revision_is_detected_from_history_that_has_no_flag(self):
        # The 2026-10-28 snapshot was written before the epoch flag existed, so
        # the only way to know about the revision is to compare it with its own
        # predecessor. Without that, a 26.10 score would be carried onto the new
        # scale and sit next to values it is not comparable with.
        def snap(day, scores):
            return {
                "generated": "%sT09:00:00+02:00" % day,
                "report": {"date": day},
                "changes": {},
                "models": [
                    {
                        "id": "cline-pass/" + name,
                        "quality": {
                            "intelligence_index": value,
                            "coding_index": 70.0,
                            "retrieved_at": "%sT09:00:00+02:00" % day,
                        },
                    }
                    for name, value in scores
                ],
            }

        before = snap(
            "2026-10-26", [("a", 44.8), ("b", 39.5), ("c", 48.1), ("d", 29.2), ("kimi-k3", 43.6)]
        )
        revision = snap(
            "2026-10-28", [("a", 31.7), ("b", 26.7), ("c", 36.4), ("d", 18.7)]
        )
        quality, evidence, _matched, carried = run.collect_quality(
            [entry("kimi-k3")], {}, {}, "2026-10-30T09:00:00+02:00", [revision, before]
        )
        self.assertEqual(0, carried)
        self.assertIsNone(quality["cline-pass/kimi-k3"])
        self.assertEqual("pre-revision", evidence["cline-pass/kimi-k3"]["reason"])
        self.assertEqual("2026-10-28", evidence["cline-pass/kimi-k3"]["revision_day"])

    def test_carry_is_bounded_by_age(self):
        entries = [entry("kimi-k3")]
        ancient = {
            "generated": "2026-08-01T09:00:00+02:00",
            "models": [
                {
                    "id": "cline-pass/kimi-k3",
                    "quality": {
                        "intelligence_index": 43.6,
                        "retrieved_at": "2026-08-01T09:00:00+02:00",
                    },
                }
            ],
        }
        quality, evidence, _matched, carried = run.collect_quality(
            entries, {}, {}, "2026-10-30T09:00:00+02:00", [ancient]
        )
        self.assertEqual(0, carried)
        self.assertIsNone(quality["cline-pass/kimi-k3"])
        self.assertEqual("unmatched", evidence["cline-pass/kimi-k3"]["via"])


class IndexEpochTests(unittest.TestCase):
    def snapshot(self, values, date_label):
        return {
            "generated": "%sT09:00:00+02:00" % date_label,
            "models": [
                {
                    "id": "cline-pass/" + name,
                    "quality": {"intelligence_index": score, "coding_index": coding},
                }
                for name, score, coding in values
            ],
        }

    def test_uniform_drop_with_frozen_coding_is_a_revision(self):
        previous = self.snapshot(
            [("a", 44.8, 74.8), ("b", 39.5, 68.0), ("c", 48.1, 70.0), ("d", 29.2, 55.0)],
            "2026-10-26",
        )
        current = self.snapshot(
            [("a", 31.7, 74.8), ("b", 26.7, 68.0), ("c", 36.4, 70.0), ("d", 18.7, 55.0)],
            "2026-10-28",
        )
        epoch = monitor.detect_quality_epoch(previous, current)
        self.assertIsNotNone(epoch)
        self.assertEqual(4, epoch["compared"])
        self.assertEqual(1.0, epoch["share_lower"])
        self.assertEqual("2026-10-26", epoch["previous_snapshot"])

    def test_single_model_regression_is_not_a_revision(self):
        previous = self.snapshot(
            [("a", 44.8, 74.8), ("b", 39.5, 68.0), ("c", 48.1, 70.0), ("d", 29.2, 55.0)],
            "2026-10-26",
        )
        current = self.snapshot(
            [("a", 31.7, 60.0), ("b", 39.5, 68.0), ("c", 48.1, 70.0), ("d", 29.2, 55.0)],
            "2026-10-28",
        )
        self.assertIsNone(monitor.detect_quality_epoch(previous, current))

    def test_drop_with_coding_also_moving_is_not_a_revision(self):
        previous = self.snapshot(
            [("a", 44.8, 74.8), ("b", 39.5, 68.0), ("c", 48.1, 70.0), ("d", 29.2, 55.0)],
            "2026-10-26",
        )
        current = self.snapshot(
            [("a", 31.7, 50.0), ("b", 26.7, 40.0), ("c", 36.4, 30.0), ("d", 18.7, 20.0)],
            "2026-10-28",
        )
        self.assertIsNone(monitor.detect_quality_epoch(previous, current))

    def test_revision_relabels_instead_of_deleting_the_numbers(self):
        changes = {
            "quality_changed": {"cline-pass/a": {"name": "a", "from": 44.8, "to": 31.7}},
            "catalog_added": [],
        }
        epoch = {"detected": True, "compared": 1, "median_change": -0.29}
        monitor.apply_quality_epoch(changes, epoch)
        self.assertEqual({}, changes["quality_changed"])
        self.assertIn("cline-pass/a", changes["quality_revision"])
        self.assertTrue(changes["quality_epoch"]["detected"])

    def test_summary_states_the_revision_first(self):
        summary = monitor.deterministic_summary(
            {
                "quality_epoch": {"detected": True, "compared": 10, "median_change": -0.29},
                "quality_changed": {},
                "catalog_added": [],
            }
        )
        self.assertTrue(summary.startswith("AA republished the Intelligence Index"))


class ReferenceTableTests(unittest.TestCase):
    def test_old_table_produces_a_warning(self):
        import datetime

        warning = monitor.reference_age_warning(
            {"retrieved_at": "2026-10-02"}, datetime.date(2026, 10, 30)
        )
        self.assertIn("28 days", warning)
        self.assertIsNone(
            monitor.reference_age_warning(
                {"retrieved_at": "2026-10-29"}, datetime.date(2026, 10, 30)
            )
        )

    def test_models_without_a_reference_entry_are_named(self):
        warning = monitor.reference_coverage_warning(
            [entry("kimi-k3"), entry("kimi-k3.1-pro")], {"kimi-k3": {"input": 1.0}}
        )
        self.assertIn("kimi-k3.1-pro", warning)
        self.assertNotIn("kimi-k3,", warning)


class RenderingTests(unittest.TestCase):
    def test_stale_score_is_rendered_with_its_date(self):
        model = {
            "id": "cline-pass/kimi-k3",
            "name": "Kimi K3",
            "family": "Kimi",
            "slug": "kimi-k3",
            "priced": True,
            "free": False,
            "rates": {},
            "quality": {
                "intelligence_index": 43.6,
                "coding_index": 61.0,
                "aa_name": "Kimi K3 (Max)",
                "retrieved_at": "2026-10-26T09:00:00+02:00",
                "fresh": False,
                "stale_since": "2026-10-26",
            },
            "quality_evidence": {"via": "carried"},
        }
        html = site._model_row(model)
        self.assertIn("stale since 2026-10-26", html)

    def test_revision_banner_replaces_the_decline_list(self):
        snapshot = {
            "changes": {
                "quality_epoch": {
                    "detected": True,
                    "compared": 10,
                    "median_change": -0.29,
                    "previous_snapshot": "2026-10-26",
                    "note": "Artificial Analysis republished the Intelligence Index.",
                },
                "quality_changed": {},
                "quality_revision": {"cline-pass/a": {"name": "a", "from": 44.8, "to": 31.7}},
            },
            "summary": "",
            "report": {"narrative": {}},
        }
        html = site._change_panel(snapshot, config())
        self.assertIn("Index revision, not a model regression", html)
        self.assertNotIn("AA quality: 44.8 to 31.7", html)


if __name__ == "__main__":
    unittest.main()
