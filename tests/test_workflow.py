"""Schedule guard and workflow wiring (section 7 step 8, the local parts)."""

import io
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone

from tests.support import ROOT, FixtureTestCase
from src import monitor, run, site

WORKFLOW = ROOT / ".github" / "workflows" / "weekly-monitor.yml"

# 2026-10-05 is a Monday inside CEST; 2026-11-02 is a Monday inside CET.
MONDAY_SUMMER_0600 = datetime(2026, 10, 5, 4, 30, tzinfo=timezone.utc)
MONDAY_WINTER_0700 = datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)
MONDAY_WINTER_0500 = datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc)
SUNDAY_SUMMER_0600 = datetime(2026, 10, 4, 4, 30, tzinfo=timezone.utc)


class ScheduleGuardTests(FixtureTestCase):
    def test_the_summer_and_winter_candidates_both_land_at_06_or_07_warsaw(self):
        allowed, reason = run.should_run_scheduled(
            self.config, self.history_dir, MONDAY_SUMMER_0600
        )
        self.assertTrue(allowed)
        self.assertIn("scheduled window matches", reason)
        self.assertTrue(
            run.should_run_scheduled(self.config, self.history_dir, MONDAY_WINTER_0700)[0]
        )

    def test_an_out_of_window_hour_is_skipped(self):
        allowed, reason = run.should_run_scheduled(
            self.config, self.history_dir, MONDAY_WINTER_0500
        )
        self.assertFalse(allowed)
        self.assertIn("scheduled hours", reason)

    def test_another_weekday_is_skipped(self):
        allowed, reason = run.should_run_scheduled(
            self.config, self.history_dir, SUNDAY_SUMMER_0600
        )
        self.assertFalse(allowed)
        self.assertIn("weekday", reason)

    def test_an_existing_live_snapshot_makes_a_repeat_invocation_skip(self):
        site.write_snapshot(self.history_dir, {"report": {"date": "2026-10-05"}}, monitor.MODE_LIVE)
        allowed, reason = run.should_run_scheduled(
            self.config, self.history_dir, MONDAY_SUMMER_0600
        )
        self.assertFalse(allowed)
        self.assertIn("already exists", reason)

    def test_an_offline_snapshot_does_not_block_the_scheduled_run(self):
        site.write_snapshot(
            self.history_dir, {"report": {"date": "2026-10-05"}}, monitor.MODE_OFFLINE
        )
        self.assertTrue(
            run.should_run_scheduled(self.config, self.history_dir, MONDAY_SUMMER_0600)[0]
        )

    def test_the_guard_uses_warsaw_time_not_the_runner_clock(self):
        # 04:30 UTC is 06:30 in Warsaw during CEST and 05:30 during CET, so the
        # same UTC instant is allowed in October and skipped in November.
        self.assertEqual(6, run.warsaw_now(self.config, MONDAY_SUMMER_0600).hour)
        self.assertEqual(5, run.warsaw_now(self.config, MONDAY_WINTER_0500).hour)
        self.assertTrue(
            run.should_run_scheduled(self.config, self.history_dir, MONDAY_SUMMER_0600)[0]
        )
        self.assertFalse(
            run.should_run_scheduled(self.config, self.history_dir, MONDAY_WINTER_0500)[0]
        )


class CliModeTests(FixtureTestCase):
    def test_a_scheduled_invocation_outside_the_window_exits_without_work(self):
        original_now = run.warsaw_now
        os.environ["GITHUB_EVENT_NAME"] = "schedule"
        buffer = io.StringIO()
        try:
            run.warsaw_now = lambda config, now=None: original_now(config, MONDAY_WINTER_0500)
            with redirect_stdout(buffer):
                code = run.main(["--history-dir", str(self.history_dir)])
        finally:
            run.warsaw_now = original_now
            os.environ.pop("GITHUB_EVENT_NAME", None)
        self.assertEqual(0, code)
        self.assertIn("SCHEDULE_SKIPPED", buffer.getvalue())
        self.assertEqual([], site.history_snapshots(self.history_dir, monitor.MODE_LIVE))

    def test_a_manual_dispatch_bypasses_the_schedule_guard(self):
        os.environ["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        for name in ("CLINE_API_KEY", "ARTIFICIAL_ANALYSIS_API_KEY"):
            os.environ.pop(name, None)
        buffer = io.StringIO()
        try:
            # Without credentials the live run stops at the API key check, which
            # proves the guard did not short-circuit the invocation.
            with redirect_stdout(buffer):
                code = run.main(["--history-dir", str(self.history_dir)])
        finally:
            os.environ.pop("GITHUB_EVENT_NAME", None)
        self.assertEqual(2, code)
        self.assertNotIn("SCHEDULE_SKIPPED", buffer.getvalue())


class WorkflowFileTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_the_workflow_schedules_both_utc_candidates_and_allows_dispatch(self):
        self.assertIn("cron: '0 4 * * 1'", self.text)
        self.assertIn("cron: '0 5 * * 1'", self.text)
        self.assertIn("workflow_dispatch:", self.text)

    def test_permissions_and_concurrency_match_the_plan(self):
        for needle in (
            "contents: write",
            "pages: write",
            "id-token: write",
            "group: weekly-monitor",
            "cancel-in-progress: false",
        ):
            self.assertIn(needle, self.text)

    def test_the_run_step_exports_the_timezone_and_the_documented_secrets(self):
        for needle in (
            "TZ: Europe/Warsaw",
            "secrets.CLINE_API_KEY",
            "secrets.ARTIFICIAL_ANALYSIS_API_KEY",
            "vars.CLINE_USER_ID",
        ):
            self.assertIn(needle, self.text)

    def test_tests_run_before_the_live_pipeline(self):
        self.assertLess(
            self.text.index("unittest discover"), self.text.index("python -m src.run")
        )

    def test_publication_uploads_the_pages_artifact_and_deploys_it(self):
        for needle in (
            "actions/upload-pages-artifact@v3",
            "actions/deploy-pages@v4",
            "name: github-pages",
            "path: site",
        ):
            self.assertIn(needle, self.text)

    def test_history_and_site_are_committed_with_the_workflow_token(self):
        for needle in ("git add data/history site", "github-actions[bot]", "git push"):
            self.assertIn(needle, self.text)

    def test_a_skipped_schedule_does_not_publish(self):
        # The two cron entries are made mutually exclusive by the app's same-day
        # snapshot check, so the workflow must detect the skip and gate both the
        # history commit and the Pages deployment on it.
        self.assertIn("SCHEDULE_SKIPPED", self.text)
        self.assertIn("id: monitor", self.text)
        self.assertIn("if: steps.monitor.outputs.skipped == 'false'", self.text)
        self.assertIn("needs.build.outputs.skipped != 'true'", self.text)

    def test_the_workflow_file_is_plain_yaml(self):
        self.assertNotIn("\t", self.text)
        self.assertIn("name:", self.text)
        self.assertIn("runs-on:", self.text)


if __name__ == "__main__":
    unittest.main()