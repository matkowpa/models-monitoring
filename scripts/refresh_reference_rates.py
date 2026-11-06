"""Refresh ``data/clinepass_reference_rates.json`` from Cline's pricing table.

The reference table is the only fallback price for a ClinePass model whose own
usage fit is not estimable, so a model added to the catalog after the table was
last touched is published with a score and no price - and therefore never
appears on the cost/quality chart. Cline documents the same rates on its
ClinePass page, which Mintlify serves as clean markdown, so the table can be
re-read instead of hand-edited.

Model rows are keyed by their documented display name: the ClinePass slug is the
lower-cased name with spaces turned into hyphens ("MiMo V2.6 Pro" ->
``mimo-v2.6-pro``). Variant rows collapse onto one entry:

* ``(Off-peak)`` / ``(Peak)`` - Peak is kept, because usage rows report one
  blended figure and the higher rate is the conservative reading.
* ``(<= N tokens)`` size tiers - the first (smallest) tier is kept and the rest
  are recorded in the entry's note.

Usage::

    python scripts/refresh_reference_rates.py --check   # report drift, exit 1
    python scripts/refresh_reference_rates.py --write   # rewrite the file
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RATES_FILE = ROOT / "data" / "clinepass_reference_rates.json"
DEFAULT_URL = "https://docs.cline.bot/getting-started/clinepass.md"
SECTION = "## Reference pricing"
MIN_MODELS = 8  # refuse an obviously broken parse

ROW_RE = re.compile(r"^\|(.+)\|\s*$")
MONEY_RE = re.compile(r"\$([0-9]+(?:\.[0-9]+)?)")
VARIANT_PAREN_RE = re.compile(r"\s*\(([^)]*)\)\s*$")
SIZE_TIER_RE = re.compile(r"^\s*[<>≤≥].*token", re.IGNORECASE)
PEAK_WORDS = {"peak", "off-peak", "off peak"}


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "models-monitoring"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def money(cell):
    """First dollar amount in a cell, or ``None`` for a dash/blank."""
    text = (cell or "").replace("\\$", "$")
    match = MONEY_RE.search(text)
    return float(match.group(1)) if match else None


def parse_rows(markdown):
    """Rows of the reference-pricing table as ``(label, input, output, read, write)``."""
    lines = markdown.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip().startswith(SECTION))
    except StopIteration:
        raise ValueError("reference pricing section not found")
    rows = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        match = ROW_RE.match(line.strip())
        if not match:
            continue
        cells = [cell.strip() for cell in match.group(1).split("|")]
        if len(cells) < 4:
            continue
        label = cells[0]
        if not label or label.lower() == "model" or set(label) <= set("-: "):
            continue
        rows.append(
            (
                label,
                money(cells[1]),
                money(cells[2]),
                money(cells[3]),
                money(cells[4]) if len(cells) > 4 else None,
            )
        )
    if not rows:
        raise ValueError("reference pricing table is empty")
    return rows


def slug_for(label):
    """ClinePass slug for a documented model name."""
    base = VARIANT_PAREN_RE.sub("", label).strip()
    return re.sub(r"\s+", "-", base.lower())


def variant_of(label):
    match = VARIANT_PAREN_RE.search(label)
    return match.group(1).strip() if match else None


def build_entries(rows):
    """Collapse the table rows onto one entry per model slug."""
    entries = {}
    for label, rate_in, rate_out, cache_read, cache_write in rows:
        slug = slug_for(label)
        variant = variant_of(label)
        entry = {
            "input": rate_in,
            "output": rate_out,
            "cache_read": cache_read,
            "cache_write": cache_write,
        }
        if variant and variant.lower() in PEAK_WORDS:
            entry["_variant"] = variant.lower()
        elif variant and SIZE_TIER_RE.match(variant):
            entry["_variant"] = variant
        else:
            entry["_variant"] = None
        current = entries.get(slug)
        if current is None or prefer(entry, current):
            entries[slug] = entry
    for entry in entries.values():
        entry.pop("_variant", None)
    return entries


def build_labels(rows):
    """Cline's published label per slug, for a catalog entry that arrives unnamed.

    A brand-new ClinePass id sometimes reaches the catalog with its slug as the
    display name ("cline-pass/qwen3.8-max"), which then labels the table row and
    the chart annotation. The pricing page carries the human label; the shortest
    variant of it is the base model name.
    """
    labels = {}
    for label, *_rest in rows:
        slug = slug_for(label)
        base = re.sub(r"\s+", " ", VARIANT_PAREN_RE.sub("", label)).strip()
        if base and (slug not in labels or len(base) < len(labels[slug])):
            labels[slug] = base
    return labels


def prefer(candidate, current):
    """Whether ``candidate`` should replace ``current`` for the same slug.

    A row without a variant marker always wins; between markers, Peak beats
    Off-peak (the conservative reading) and a size tier without a ``>`` beats
    one with it (the cheaper, smaller-context tier).
    """
    new, old = candidate.get("_variant"), current.get("_variant")
    if new is None:
        return True
    if old is None:
        return False
    if new in PEAK_WORDS or old in PEAK_WORDS:
        return new == "peak"
    new_big = str(new).lstrip().startswith(">")
    old_big = str(old).lstrip().startswith(">")
    return old_big and not new_big


def load():
    return json.loads(RATES_FILE.read_text(encoding="utf-8"))


def diff(document, entries):
    """Human-readable drift between the stored table and the page."""
    stored = document.get("models") or {}
    added = [slug for slug in entries if slug not in stored]
    changed = []
    for slug, entry in entries.items():
        old = stored.get(slug)
        if old is None:
            continue
        for field in ("input", "output", "cache_read", "cache_write"):
            if old.get(field) != entry.get(field):
                changed.append("%s.%s: %s -> %s" % (slug, field, old.get(field), entry.get(field)))
    gone = [slug for slug in stored if slug not in entries]
    return added, changed, gone


def apply(document, entries, today, labels=None):
    stored = document.setdefault("models", {})
    for slug, entry in entries.items():
        existing = stored.get(slug) or {}
        updated = dict(existing)
        updated.update({key: value for key, value in entry.items()})
        # Keep hand-written explanations that describe a variant choice.
        for key in ("note", "off_peak"):
            if key in existing:
                updated.setdefault(key, existing[key])
        stored[slug] = updated
    if labels:
        document["labels"] = dict(sorted(labels.items()))
    if document.get("retrieved_at") != today:
        document["retrieved_at"] = today
    return document


def _inline(value):
    """One-line JSON for a nested block, so a model entry stays readable."""
    if isinstance(value, dict):
        return "{%s}" % ", ".join(
            "%s: %s" % (json.dumps(key), _inline(item)) for key, item in value.items()
        )
    return json.dumps(value, ensure_ascii=False)


def dump_document(document):
    """Serialize the rates file with one line per model entry.

    ``json.dumps(indent=2)`` would explode every four-field model entry into six
    lines, turning a one-model refresh into a hundred-line diff. The weekly
    workflow commits this file, so the diff has to show only what changed.
    """
    lines = ["{"]
    keys = list(document)
    for position, key in enumerate(keys):
        comma = "," if position < len(keys) - 1 else ""
        value = document[key]
        if key == "models" and isinstance(value, dict):
            lines.append('  "models": {')
            slugs = list(value)
            for index, slug in enumerate(slugs):
                entry = value[slug]
                inner = ", ".join(
                    "%s: %s" % (json.dumps(field), _inline(item))
                    for field, item in entry.items()
                )
                lines.append(
                    "    %s: { %s }%s"
                    % (json.dumps(slug), inner, "," if index < len(slugs) - 1 else "")
                )
            lines.append("  }" + comma)
            continue
        body_lines = json.dumps(value, indent=2, ensure_ascii=False).splitlines()
        if len(body_lines) == 1:
            lines.append("  %s: %s%s" % (json.dumps(key), body_lines[0], comma))
            continue
        lines.append("  %s: %s" % (json.dumps(key), body_lines[0]))
        for extra in body_lines[1:-1]:
            lines.append("  " + extra)
        lines.append("  " + body_lines[-1] + comma)
    lines.append("}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the rates file")
    parser.add_argument("--check", action="store_true", help="exit 1 when the file is stale")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--input", help="parse a local markdown file instead of the page")
    args = parser.parse_args(argv)
    try:
        markdown = Path(args.input).read_text(encoding="utf-8") if args.input else fetch(args.url)
        rows = parse_rows(markdown)
        entries = build_entries(rows)
    except (OSError, ValueError) as error:
        print("RATES_FAILED: %s" % error, file=sys.stderr)
        return 2
    if len(entries) < MIN_MODELS:
        print("RATES_FAILED: only %d models parsed" % len(entries), file=sys.stderr)
        return 2
    for slug, entry in entries.items():
        if not entry["input"] or not entry["output"]:
            print("RATES_FAILED: %s has no input/output rate" % slug, file=sys.stderr)
            return 2
    document = load()
    added, changed, gone = diff(document, entries)
    print(
        "RATES: %d model(s) on the page, %d stored, %d new, %d changed, %d not listed"
        % (len(entries), len(document.get("models") or {}), len(added), len(changed), len(gone))
    )
    for slug in added:
        print("  new: %s %s" % (slug, entries[slug]))
    for line in changed:
        print("  drift: %s" % line)
    for slug in gone:
        print("  not listed (kept): %s" % slug)
    if args.write:
        apply(document, entries, date.today().isoformat(), labels=build_labels(rows))
        RATES_FILE.write_text(dump_document(document), encoding="utf-8")
        print("WROTE %s" % RATES_FILE)
        return 0
    if args.check and (added or changed):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
