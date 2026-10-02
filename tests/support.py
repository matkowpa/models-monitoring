"""Shared helpers for the offline test suite.

Every test runs without credentials and without network access: fixtures stand in
for Cline, Artificial Analysis, and OpenRouter, which is the boundary
models_monitoring.md section 7 requires.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import monitor  # noqa: E402
from src import run  # noqa: E402
from src import site  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def load(name):
    """Load a JSON fixture, falling back to the tracked data/ files."""
    for base in (FIXTURES, ROOT / "data"):
        path = base / name
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(name)


def config():
    return run.load_config()


def ground_truth():
    return load("usage_ground_truth.json")["rates"]


def fixture_rows():
    return run.load_fixture_usage_rows(FIXTURES)


def normalized_fixture_rows():
    rows, dropped = monitor.normalize_usage_rows(fixture_rows(), config()["money_scales"])
    return rows, dropped


def paid_entries():
    """Catalog entries from the fixture payload, paid only."""
    return [
        entry
        for entry in monitor.parse_recommended_models(load("recommended_models.json"))
        if not entry["free"]
    ]


class FixtureTestCase(unittest.TestCase):
    """Base class that points the site and history output at a temporary tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.site_dir = self.tmp / "site"
        self.history_dir = self.tmp / "history"
        self.config = config()

    def tearDown(self):
        self._tmp.cleanup()

    def run_offline(self, **kwargs):
        snapshot, site_dir, history_dir = run.run_report(
            self.config,
            mode=monitor.MODE_OFFLINE,
            site_dir=self.site_dir,
            history_dir=self.history_dir,
            fixtures_dir=FIXTURES,
            **kwargs
        )
        run.publish_report(self.config, snapshot, site_dir, history_dir, monitor.MODE_OFFLINE)
        return snapshot
