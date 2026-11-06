"""Dump the Artificial Analysis model list as a workflow artifact.

The weekly monitor only keeps the records it matched, so an unmatched catalog
entry gives no clue whether AA publishes it under another name or not at all.
This dump records the whole list (slug, name, creator, evaluation keys, index)
so matching can be diagnosed offline. The free-tier list is used because it
answers for any key tier; it is never published, only uploaded as an artifact.

Usage: ``python scripts/aa_catalog_dump.py [output.json]`` with
``ARTIFICIAL_ANALYSIS_API_KEY`` in the environment.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import monitor, run  # noqa: E402  (imported after ROOT is importable)


def main(argv):
    config = run.load_config()
    aa = config.get("artificial_analysis") or {}
    key = os.environ.get(aa.get("api_key_env", "ARTIFICIAL_ANALYSIS_API_KEY"), "")
    if not key:
        print("AA key is not set; nothing to dump", file=sys.stderr)
        return 2
    endpoint = aa.get("free_endpoint") or aa["endpoint"].rstrip("/") + "/free"
    payload = run.fetch_aa_pages(
        endpoint,
        {aa.get("header_name", "x-api-key"): key},
        timeout=aa.get("request_timeout_seconds", 30),
        retries=aa.get("max_retries", 2),
        max_pages=aa.get("max_pages", 25),
    )
    records = []
    evaluation_keys = set()
    for item in payload["data"]:
        evaluations = item.get("evaluations") or {}
        if isinstance(evaluations, dict):
            evaluation_keys.update(evaluations.keys())
        records.append(
            {
                "slug": item.get("slug"),
                "name": item.get("name"),
                "id": item.get("id"),
                "creator": (item.get("model_creator") or {}).get("name"),
                "release_date": item.get("release_date"),
                "evaluations": evaluations if isinstance(evaluations, dict) else {},
                "normalized": monitor.normalize_key(item.get("name")),
                "identity": monitor.identity_key(item.get("name")),
            }
        )
    output = {
        "endpoint": endpoint,
        "tier": payload.get("tier"),
        "pages": payload.get("pages_fetched"),
        "count": len(records),
        "evaluation_keys": sorted(evaluation_keys),
        "data": records,
    }
    target = Path(argv[0] if argv else "aa-catalog.json")
    target.write_text(json.dumps(output, indent=1, ensure_ascii=False), encoding="utf-8")
    print(
        "AA_DUMP: %d model(s), %s page(s), tier=%s, evaluation keys=%s -> %s"
        % (
            len(records),
            payload.get("pages_fetched"),
            payload.get("tier"),
            ",".join(output["evaluation_keys"]),
            target,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
