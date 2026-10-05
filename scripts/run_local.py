"""Local entry point: run the pipeline with the secrets stored in ``.env``.

``src/run.py`` reads the environment only, because that is how the weekly
workflow supplies its GitHub secrets. This launcher is the local operator's
equivalent: it loads ``.env`` from the repository root and then calls
``src.run.main`` unchanged. Neither the workflow nor the test suite imports it,
so a ``.env`` can never change a test result or a scheduled run.

Values already present in the environment win over ``.env``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

# "python scripts/run_local.py" puts scripts/ on sys.path, not the repository root.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import run  # noqa: E402  (imported after ROOT is importable)


def parse_env(text):
    """The ``KEY=VALUE`` pairs in a ``.env`` body, ignoring blanks and comments."""
    values = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value:
            values[key] = value
    return values


def load_env(environ=None, path=None):
    """Copy ``.env`` into ``environ`` without overwriting existing values.

    Returns the names that were set, so the caller can report them without ever
    echoing a secret value. An empty placeholder is left alone, which keeps an
    exported variable authoritative.
    """
    environ = os.environ if environ is None else environ
    env_path = Path(path or ENV_FILE)
    if not env_path.exists():
        return []
    loaded = []
    for key, value in parse_env(env_path.read_text(encoding="utf-8")).items():
        if environ.get(key):
            continue
        environ[key] = value
        loaded.append(key)
    return loaded


def main(argv=None):
    loaded = load_env()
    if loaded:
        print("run_local: loaded %s from %s" % (", ".join(loaded), ENV_FILE))
    else:
        print("run_local: no new values in %s" % ENV_FILE)
    return run.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
