"""Rate-limit handling, usage paging, and workflow failure wiring.

These are the regressions the 2026-10-16..21 silent outage asked for: Cline
answered 429 halfway through the usage walk, the retry budget was 1.5 s, and the
workflow's ``python -m src.run | tee run.log`` swallowed the non-zero exit, so
six scheduled runs reported success while publishing nothing.
"""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tests.support import ROOT, config
from src import monitor, run

WORKFLOW = ROOT / ".github" / "workflows" / "weekly-monitor.yml"
BASH = shutil.which("bash")


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def http_error(code, headers=None):
    return urllib.error.HTTPError(
        "https://example.test/x", code, "boom", headers or {}, io.BytesIO(b"")
    )


class RateLimitRetryTests(unittest.TestCase):
    """A 429 must be waited out, not treated as a two-second blip."""

    def call(self, errors, **kwargs):
        """Run http_json against a scripted sequence; return (result, sleeps)."""
        sequence = list(errors)
        sleeps = []

        def fake_urlopen(request, timeout=None):
            item = sequence.pop(0)
            if isinstance(item, Exception):
                raise item
            return FakeResponse(item)

        with mock.patch.object(run.urllib.request, "urlopen", fake_urlopen):
            with mock.patch.object(run, "_sleep", sleeps.append):
                result = run.http_json("https://example.test/x", **kwargs)
        return result, sleeps

    def test_retry_after_is_honoured_and_the_call_succeeds(self):
        result, sleeps = self.call(
            [http_error(429, {"Retry-After": "7"}), {"ok": True}]
        )
        self.assertEqual({"ok": True}, result)
        self.assertEqual([7.0], sleeps)

    def test_without_a_header_the_rate_limit_backoff_doubles(self):
        result, sleeps = self.call(
            [http_error(429), http_error(429), {"ok": True}],
            rate_limit_backoff=2.0,
        )
        self.assertEqual({"ok": True}, result)
        self.assertEqual([2.0, 4.0], sleeps)

    def test_retry_after_is_capped_so_a_run_cannot_hang(self):
        _result, sleeps = self.call(
            [http_error(429, {"Retry-After": "600"}), {"ok": True}]
        )
        self.assertEqual([run.MAX_RETRY_DELAY_SECONDS], sleeps)

    def test_the_rate_limit_budget_is_bounded_and_then_fails(self):
        errors = [http_error(429) for _ in range(10)]
        with self.assertRaises(run.HttpError):
            self.call(errors, rate_limit_retries=3, rate_limit_backoff=0.01)

    def test_a_rate_limit_does_not_consume_the_ordinary_retry_budget(self):
        # 429 then 500 then success: the 500 still has its own two retries.
        result, sleeps = self.call(
            [http_error(429), http_error(500), {"ok": True}],
            rate_limit_backoff=2.0,
            backoff=0.5,
            retries=2,
        )
        self.assertEqual({"ok": True}, result)
        self.assertEqual([2.0, 0.5], sleeps)

    def test_a_server_error_keeps_the_short_budget(self):
        with self.assertRaises(run.HttpError):
            self.call([http_error(503), http_error(503), http_error(503)], retries=2)

    def test_a_post_uses_the_same_policy(self):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["method"] = request.get_method()
            seen["content_type"] = request.get_header("Content-type")
            if not seen.get("raised"):
                seen["raised"] = True
                raise http_error(429, {"Retry-After": "3"})
            return FakeResponse({"choices": []})

        sleeps = []
        with mock.patch.object(run.urllib.request, "urlopen", fake_urlopen):
            with mock.patch.object(run, "_sleep", sleeps.append):
                result = run.post_json_chat(
                    "https://example.test/chat", headers={}, payload={"model": "m"}
                )
        self.assertEqual({"choices": []}, result)
        self.assertEqual([3.0], sleeps)
        self.assertEqual("POST", seen["method"])
        self.assertEqual("application/json", seen["content_type"])


class UsagePagingTests(unittest.TestCase):
    """The walk stops at the window edge and paces itself."""

    def setUp(self):
        os.environ["CLINE_API_KEY"] = "test-key"
        self.config = config()
        self.urls = []
        self.sleeps = []
        self._http_json = run.http_json
        self._sleep = run._sleep
        run._sleep = self.sleeps.append

    def tearDown(self):
        run.http_json = self._http_json
        run._sleep = self._sleep
        os.environ.pop("CLINE_API_KEY", None)

    def serve(self, pages):
        """Serve scripted usage pages; pages are (items, next_token) tuples."""
        queue = list(pages)

        def fake_http_json(url, **kwargs):
            self.urls.append(url)
            if url.endswith("/users/me"):
                return {"data": {"id": "usr-1"}, "success": True}
            items, token = queue.pop(0)
            return {"data": {"items": items, "nextToken": token}, "success": True}

        run.http_json = fake_http_json

    @staticmethod
    def rows(*dates):
        return [{"id": "%s-%d" % (value, index), "createdAt": value + "T06:00:00Z"}
                for index, value in enumerate(dates)]

    def test_the_walk_stops_once_the_window_start_is_covered(self):
        self.serve(
            [
                (self.rows("2026-10-21", "2026-10-20"), "c2"),
                (self.rows("2026-10-02", "2026-09-28"), "c3"),
            ]
        )
        rows, stats = run.fetch_usage_rows(self.config, window_start="2026-10-01")
        self.assertEqual(4, len(rows))
        self.assertEqual(2, stats["pages"])
        self.assertTrue(stats["stopped_at_window_start"])
        self.assertEqual(4, stats["rows_fetched"])
        self.assertNotIn("c3", self.urls[-1])

    def test_an_oldest_first_page_never_truncates_the_history(self):
        # Ascending dates: the ordering is not the documented one, so the walk
        # must keep going rather than trust a min() over the page.
        self.serve(
            [
                (self.rows("2026-09-01", "2026-10-21"), "c2"),
                ([], None),
            ]
        )
        _rows, stats = run.fetch_usage_rows(self.config, window_start="2026-10-01")
        self.assertEqual(2, stats["pages"])
        self.assertFalse(stats["stopped_at_window_start"])

    def test_pages_are_paced_and_request_the_capped_page_size(self):
        self.serve(
            [
                (self.rows("2026-10-21"), "c2"),
                (self.rows("2026-10-01", "2026-09-30"), None),
            ]
        )
        run.fetch_usage_rows(self.config, window_start="2026-10-01")
        usage_urls = [url for url in self.urls if "/usages?" in url]
        self.assertEqual(2, len(usage_urls))
        self.assertTrue(all("limit=200" in url for url in usage_urls))
        # One pause between the two pages, none before the first request.
        self.assertEqual([self.config["cline"]["usage_page_pause_seconds"]], self.sleeps)

    def test_the_page_ceiling_fails_loudly_instead_of_looping(self):
        config = json.loads(json.dumps(self.config))
        config["cline"]["usage_max_pages"] = 2
        config["cline"]["usage_page_pause_seconds"] = 0
        self.serve(
            [
                (self.rows("2026-10-21"), "c2"),
                (self.rows("2026-10-20"), "c3"),
                (self.rows("2026-10-19"), "c4"),
            ]
        )
        with self.assertRaises(run.HttpError) as caught:
            run.fetch_usage_rows(config, window_start="2026-10-01")
        self.assertIn("more than 2 usage pages", str(caught.exception))

    def test_a_page_without_dates_does_not_stop_the_walk(self):
        self.serve(
            [
                ([{"id": "u1"}], "c2"),
                (self.rows("2026-10-21"), None),
            ]
        )
        _rows, stats = run.fetch_usage_rows(self.config, window_start="2026-10-01")
        self.assertEqual(2, stats["pages"])


class WorkflowTextMixin:
    """Shared access to the workflow file and its "Run the monitor" body."""

    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def monitor_run_block(self):
        """The literal shell body of the "Run the monitor" step."""
        block = self.text.split("- name: Run the monitor", 1)[1]
        block = block.split("- name:", 1)[0]
        lines = block.splitlines()
        start = [index for index, line in enumerate(lines) if line.strip() == "run: |"][0]
        return "\n".join(
            line[10:] for line in lines[start + 1 :] if line.startswith("          ")
        )


class WorkflowFailureWiringTests(WorkflowTextMixin, unittest.TestCase):
    """The run step must turn a refused publication into a red build."""

    def test_the_run_step_turns_on_pipefail(self):
        self.assertIn("set -o pipefail", self.text)

    def test_the_step_pipes_stderr_into_the_log_it_checks(self):
        # src/run.py writes RUN_FAILED to stderr, so a plain `| tee run.log`
        # would leave the guard grepping a file that can never contain it.
        self.assertIn("python -m src.run 2>&1 | tee run.log", self.text)

    def test_the_run_step_fails_on_a_run_failed_line(self):
        self.assertIn("grep -q '^RUN_FAILED' run.log", self.text)

    def test_the_log_is_kept_when_the_job_fails(self):
        self.assertIn("actions/upload-artifact@v4", self.text)
        self.assertIn("name: run-log", self.text)
        self.assertIn("if: failure()", self.text)


@unittest.skipUnless(BASH, "bash is required to check pipeline semantics")
class RunStepSemanticsTests(WorkflowTextMixin, unittest.TestCase):
    """Execute the real workflow snippet against a stubbed interpreter."""

    def run_step(self, stub_body):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stub_dir = tmp_path / "bin"
            stub_dir.mkdir()
            stub = stub_dir / "python"
            stub.write_text("#!/bin/sh\n" + stub_body + "\n", encoding="utf-8")
            stub.chmod(0o755)
            output = tmp_path / "github_output"
            output.write_text("", encoding="utf-8")
            env = dict(os.environ)
            env["PATH"] = str(stub_dir) + os.pathsep + env.get("PATH", "")
            env["GITHUB_OUTPUT"] = str(output)
            result = subprocess.run(
                [BASH, "-e", "-c", self.monitor_run_block()],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
            )
            return result, output.read_text(encoding="utf-8")

    def test_a_pipeline_without_pipefail_hides_a_failed_run(self):
        # The old step: the pipeline's status is tee's, so this exits 0.
        masked = subprocess.run(
            [BASH, "-e", "-c", "false | tee /dev/null"], capture_output=True, text=True
        )
        self.assertEqual(0, masked.returncode)

    def test_a_non_zero_exit_fails_the_step(self):
        result, output_text = self.run_step("exit 2")
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("skipped=false", output_text)

    def test_a_run_failed_line_fails_the_step_even_on_exit_zero(self):
        result, output_text = self.run_step('echo "RUN_FAILED: HTTP 429 from test" >&2; exit 0')
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("skipped=false", output_text)

    def test_a_completed_run_still_marks_the_job_as_not_skipped(self):
        result, output_text = self.run_step('echo "mode=live date=2026-10-21"; exit 0')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("skipped=false", output_text)

    def test_a_skipped_schedule_publishes_nothing_but_stays_green(self):
        result, output_text = self.run_step('echo "SCHEDULE_SKIPPED: already exists"; exit 0')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("skipped=true", output_text)


class WindowStartTests(unittest.TestCase):
    def test_a_page_older_than_the_window_stops_the_walk(self):
        items = [{"createdAt": "2026-10-02T00:00:00Z"}, {"createdAt": "2026-09-01T00:00:00Z"}]
        self.assertTrue(run._page_reached_window_start(items, "2026-10-01"))

    def test_a_page_still_inside_the_window_does_not(self):
        items = [{"createdAt": "2026-10-21T00:00:00Z"}, {"createdAt": "2026-10-20T00:00:00Z"}]
        self.assertFalse(run._page_reached_window_start(items, "2026-10-01"))

    def test_a_page_without_dates_does_not(self):
        self.assertFalse(run._page_reached_window_start([{"id": "u1"}], "2026-10-01"))

    def test_without_a_window_nothing_stops(self):
        items = [{"createdAt": "2020-01-01T00:00:00Z"}]
        self.assertFalse(run._page_reached_window_start(items, None))


class ObservationTests(unittest.TestCase):
    def test_the_snapshot_records_how_the_walk_ended(self):
        source = (ROOT / "src" / "run.py").read_text(encoding="utf-8")
        for key in ("rows_fetched", "paging_stopped_at_window_start"):
            self.assertIn('"%s"' % key, source)


if __name__ == "__main__":
    unittest.main()
