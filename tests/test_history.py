"""Snapshot history and archive behaviour (section 7 step 7)."""

import unittest

from tests.support import FixtureTestCase
from src import monitor, site


class SnapshotHistoryTests(FixtureTestCase):
    def test_offline_and_live_snapshots_use_different_filenames(self):
        self.assertEqual(
            "2026-10-02.json", site.snapshot_filename("2026-10-02", monitor.MODE_LIVE)
        )
        self.assertEqual(
            "2026-10-02.offline.json",
            site.snapshot_filename("2026-10-02", monitor.MODE_OFFLINE),
        )

    def test_write_and_read_round_trip(self):
        path = site.write_snapshot(
            self.history_dir, {"report": {"date": "2026-09-28"}, "models": []}, monitor.MODE_LIVE
        )
        self.assertTrue(path.exists())
        self.assertEqual("2026-09-28.json", path.name)
        self.assertEqual([], site.read_snapshot(path)["models"])

    def test_an_offline_run_does_not_block_a_live_one_for_the_same_day(self):
        self.run_offline()
        self.assertTrue(
            site.snapshot_exists(self.history_dir, "2026-10-02", monitor.MODE_OFFLINE)
        )
        self.assertEqual(
            [], site.history_snapshots(self.history_dir, monitor.MODE_LIVE)
        )

    def test_history_lists_only_the_requested_mode_newest_first(self):
        for day in ("2026-09-21", "2026-09-28", "2026-10-05"):
            site.write_snapshot(self.history_dir, {"report": {"date": day}}, monitor.MODE_LIVE)
        site.write_snapshot(
            self.history_dir, {"report": {"date": "2026-10-01"}}, monitor.MODE_OFFLINE
        )
        names = [path.name for path in site.history_snapshots(self.history_dir, monitor.MODE_LIVE)]
        self.assertEqual(["2026-10-05.json", "2026-09-28.json", "2026-09-21.json"], names)

    def test_previous_snapshot_selection_uses_the_latest_earlier_date(self):
        for day in ("2026-09-21", "2026-09-28", "2026-10-05"):
            site.write_snapshot(
                self.history_dir,
                {"report": {"date": day}, "models": [{"id": day}]},
                monitor.MODE_LIVE,
            )
        previous, path = site.find_previous_snapshot(
            self.history_dir, "2026-10-01", monitor.MODE_LIVE
        )
        self.assertEqual("2026-09-28.json", path.name)
        self.assertEqual("2026-09-28", previous["models"][0]["id"])

    def test_previous_snapshot_can_exclude_the_current_file(self):
        site.write_snapshot(
            self.history_dir, {"report": {"date": "2026-10-02"}}, monitor.MODE_LIVE
        )
        previous, path = site.find_previous_snapshot(
            self.history_dir, "2026-10-02", monitor.MODE_LIVE, exclude="2026-10-02.json"
        )
        self.assertIsNone(path)
        self.assertIsNone(previous)

    def test_missing_history_directory_is_not_an_error(self):
        self.assertEqual([], site.history_snapshots(self.history_dir / "nope", monitor.MODE_LIVE))
        self.assertIsNone(
            site.find_previous_snapshot(self.history_dir, "2026-10-02", monitor.MODE_LIVE)[0]
        )


class ArchiveTests(FixtureTestCase):
    def test_archive_entries_are_newest_first_and_deduplicated_by_date(self):
        archive_path = self.site_dir / site.ARCHIVE_FILE
        for day in ("2026-09-21", "2026-09-28"):
            site.update_archive(archive_path, {"date": day, "report": day + ".html"}, 10)
        site.update_archive(archive_path, {"date": "2026-09-28", "report": "updated.html"}, 10)
        entries = site.read_archive(archive_path)
        self.assertEqual(["2026-09-28", "2026-09-21"], [entry["date"] for entry in entries])
        self.assertEqual("updated.html", entries[0]["report"])

    def test_archive_keeps_only_the_configured_number_of_entries(self):
        archive_path = self.site_dir / site.ARCHIVE_FILE
        for index in range(6):
            site.update_archive(archive_path, {"date": "2026-09-%02d" % (index + 1)}, 3)
        entries = site.read_archive(archive_path)
        self.assertEqual(3, len(entries))
        self.assertEqual("2026-09-06", entries[0]["date"])

    def test_a_corrupt_archive_file_is_replaced_rather_than_fatal(self):
        archive_path = self.site_dir / site.ARCHIVE_FILE
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_text("{ not json", encoding="utf-8")
        self.assertEqual([], site.read_archive(archive_path))
        self.assertEqual(1, len(site.update_archive(archive_path, {"date": "2026-09-21"}, 5)))

    def test_offline_run_writes_archive_entry_and_dated_report(self):
        snapshot = self.run_offline()
        entries = site.read_archive(self.site_dir / site.ARCHIVE_FILE)
        self.assertEqual(1, len(entries))
        entry = entries[0]
        self.assertEqual(snapshot["report"]["date"], entry["date"])
        self.assertEqual(monitor.MODE_OFFLINE, entry["mode"])
        self.assertEqual("deterministic", entry["narrative_model"])
        self.assertEqual("%s.offline.json" % entry["date"], entry["snapshot"])
        self.assertIn("counts", entry)

    def test_a_second_run_on_the_same_date_replaces_the_archive_entry(self):
        self.run_offline()
        self.run_offline()
        self.assertEqual(1, len(site.read_archive(self.site_dir / site.ARCHIVE_FILE)))

    def test_dashboard_links_every_archived_report(self):
        snapshot = self.run_offline()
        dashboard = (self.site_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="archive/%s.html"' % snapshot["report"]["date"], dashboard)
        self.assertIn('href="archive/index.html"', dashboard)
        self.assertIn('href="methodology.html"', dashboard)
        self.assertIn("artificialanalysis.ai", dashboard)

    def test_archive_index_lists_entries_and_links_back(self):
        snapshot = self.run_offline()
        index = (self.site_dir / "archive" / "index.html").read_text(encoding="utf-8")
        self.assertIn(snapshot["report"]["date"], index)
        self.assertIn('href="../index.html"', index)
        self.assertIn(snapshot["report"]["date"] + ".offline.json", index)

    def test_snapshot_copy_on_the_site_matches_the_history_file(self):
        snapshot = self.run_offline()
        copy = (self.site_dir / "snapshot.json").read_text(encoding="utf-8")
        history = (self.history_dir / ("%s.offline.json" % snapshot["report"]["date"])).read_text(
            encoding="utf-8"
        )
        self.assertEqual(history, copy)


if __name__ == "__main__":
    unittest.main()