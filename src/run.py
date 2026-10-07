"""Orchestration and external calls for the ClinePass models report.

This module owns every network call (Cline recommended-models and billing usage,
Artificial Analysis, OpenRouter, and the narrative chat completions), plus the
run modes, the schedule guard, and the CLI. Parsing, fitting, and scoring stay in
``src/monitor.py``, and rendering stays in ``src/site.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import monitor
from . import site as site_module

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
SITE_DIR = ROOT / "site"

RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    """A non-retryable HTTP failure after retries."""


def http_json(url, headers=None, timeout=30, retries=2, backoff=0.5, parse=monitor.ApiError):
    """GET a URL and return parsed JSON, retrying transient failures."""
    header_block = {"Accept": "application/json"}
    header_block.update(headers or {})
    last_error = None
    for attempt in range(max(1, retries + 1)):
        try:
            request = urllib.request.Request(url, headers=header_block)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code in RETRYABLE_STATUSES and attempt < retries:
                time.sleep(backoff * (2**attempt))
                last_error = HttpError("HTTP %s from %s" % (error.code, url))
                continue
            raise HttpError("HTTP %s from %s" % (error.code, url)) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt < retries:
                time.sleep(backoff * (2**attempt))
                last_error = error
                continue
            raise HttpError("%s while requesting %s" % (error, url)) from error
        except ValueError as error:
            raise HttpError("invalid JSON from %s" % url) from error
    raise HttpError(str(last_error))


def post_json_chat(url, headers, payload, timeout=30, retries=2, backoff=0.5):
    """POST a chat completion and return parsed JSON with the same retry policy."""
    header_block = {"Content-Type": "application/json"}
    header_block.update(headers or {})
    body = json.dumps(payload).encode("utf-8")
    last_error = None
    for attempt in range(max(1, retries + 1)):
        try:
            request = urllib.request.Request(url, data=body, headers=header_block, method="POST")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code in RETRYABLE_STATUSES and attempt < retries:
                time.sleep(backoff * (2**attempt))
                last_error = HttpError("HTTP %s from %s" % (error.code, url))
                continue
            raise HttpError("HTTP %s from %s" % (error.code, url)) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt < retries:
                time.sleep(backoff * (2**attempt))
                last_error = error
                continue
            raise HttpError("%s while requesting %s" % (error, url)) from error
        except ValueError as error:
            raise HttpError("invalid JSON from %s" % url) from error
    raise HttpError(str(last_error))


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_config(path=None):
    return load_json(path or (DATA_DIR / "scoring_config.json"))


# ---------------------------------------------------------------------------
# External data collection
# ---------------------------------------------------------------------------


def auth_headers(api_key):
    return {"Authorization": "Bearer %s" % api_key}


def require_env(config, section, key_name):
    value = os.environ.get(key_name, "")
    if not value:
        raise HttpError(
            "%s is not set; live runs need a Cline API key from app.cline.bot "
            "(Settings > API Keys)" % key_name
        )
    return value


def fetch_recommended_models(config):
    """Discover the ClinePass catalog, or fall back to the pinned list.

    Returns ``(entries, source, reason)`` where source is ``recommended-models``
    or ``fallback``; a fallback run is labelled as such in the report and the
    snapshot and must never be described as live discovery.
    """
    cline = config.get("cline") or {}
    base = cline.get("api_base_url", "https://api.cline.bot")
    url = base + cline.get("recommended_models_path", "/api/v1/ai/cline/recommended-models")
    api_key = os.environ.get(cline.get("api_key_env", "CLINE_API_KEY"), "")
    headers = auth_headers(api_key) if api_key else None
    entries = None
    error = None
    try:
        payload = http_json(
            url,
            headers=headers,
            timeout=cline.get("request_timeout_seconds", 30),
            retries=cline.get("max_retries", 2),
        )
        entries = monitor.parse_recommended_models(payload)
    except (HttpError, monitor.ApiError) as caught:
        error = caught
    if not monitor.has_paid_models(entries or []):
        fallback = monitor.parse_catalog_fallback(
            load_json(DATA_DIR / "clinepass_catalog_fallback.json")
        )
        reason = str(error) if error else "recommended-models returned no clinePass entries"
        return fallback, monitor.CATALOG_SOURCE_FALLBACK, reason
    return entries, monitor.CATALOG_SOURCE_LIVE, None


def fetch_openrouter_metadata(config):
    """Optional OpenRouter metadata; None when unavailable."""
    openrouter = config.get("openrouter") or {}
    api_key = os.environ.get(openrouter.get("api_key_env", "OPENROUTER_API_KEY"), "")
    headers = {"Authorization": "Bearer %s" % api_key} if api_key else None
    try:
        payload = http_json(
            openrouter.get("endpoint", "https://openrouter.ai/api/v1/models"),
            headers=headers,
            timeout=openrouter.get("request_timeout_seconds", 30),
            retries=1,
        )
        return monitor.parse_openrouter_models(payload)
    except (HttpError, monitor.ApiError):
        return None


def fetch_usage_rows(config):
    """All billing usage rows for this account, following the server cursor.

    ``GET /users/{id}/usages`` is paged with ``nextToken``; a repeated cursor is
    treated as a server fault rather than an infinite loop. ``GET /users/me``
    resolves the account id and is checked against ``CLINE_USER_ID`` when that
    variable is set, so the report can never price the wrong account silently.
    """
    cline = config.get("cline") or {}
    base = cline.get("api_base_url", "https://api.cline.bot")
    api_key = require_env(config, "cline", cline.get("api_key_env", "CLINE_API_KEY"))
    headers = auth_headers(api_key)
    timeout = cline.get("request_timeout_seconds", 30)
    retries = cline.get("max_retries", 2)
    limit = cline.get("usage_page_limit", 1000)

    me = monitor.unwrap_envelope(
        http_json(base + "/api/v1/users/me", headers=headers, timeout=timeout, retries=retries),
        context="users/me",
    )
    user_id = str((me or {}).get("id") or "")
    if not user_id:
        raise HttpError("users/me returned no id")
    expected = os.environ.get(cline.get("user_id_env", "CLINE_USER_ID"), "")
    if expected and expected != user_id:
        raise HttpError(
            "%s (%s) does not match the account id returned by users/me (%s)"
            % (cline.get("user_id_env", "CLINE_USER_ID"), expected, user_id)
        )

    rows = []
    seen = set()
    pages = 0
    cursor = None
    while True:
        query = {"limit": str(limit)}
        if cursor:
            query["cursor"] = cursor
        payload = http_json(
            base + "/api/v1/users/%s/usages?%s" % (user_id, urllib.parse.urlencode(query)),
            headers=headers,
            timeout=timeout,
            retries=retries,
        )
        items, next_token, _total = monitor.parse_usage_page(payload)
        rows.extend(items)
        pages += 1
        cursor = next_token
        if not cursor:
            break
        if cursor in seen:
            raise HttpError("Cline API returned a repeated pagination cursor")
        seen.add(cursor)
    return rows, {"user_id": user_id, "pages": pages}


def fetch_aa_models(config):
    """The Artificial Analysis feed; raises when the key is missing or it fails."""
    aa = config.get("artificial_analysis") or {}
    api_key = os.environ.get(aa.get("api_key_env", "ARTIFICIAL_ANALYSIS_API_KEY"), "")
    if not api_key:
        raise HttpError(
            "%s is not set; live scored publication requires an Artificial Analysis key"
            % aa.get("api_key_env", "ARTIFICIAL_ANALYSIS_API_KEY")
        )
    payload = http_json(
        aa.get("endpoint", "https://artificialanalysis.ai/api/v2/data/llms/models"),
        headers={aa.get("header_name", "x-api-key"): api_key},
        timeout=aa.get("request_timeout_seconds", 30),
        retries=aa.get("max_retries", 2),
    )
    return monitor.parse_aa_models(
        payload,
        intelligence_key=aa.get("intelligence_index_key", "artificial_analysis_intelligence_index"),
        coding_key=aa.get("coding_index_key", "artificial_analysis_coding_index"),
        default_scale=aa.get("index_scale"),
    )


# ---------------------------------------------------------------------------
# Narrative model and schedule guard
# ---------------------------------------------------------------------------


def extract_summary_text(payload):
    """The assistant text from an OpenAI-compatible chat completion.

    ClinePass chat completions can arrive wrapped in the same ``{ data, success }``
    envelope the account endpoints use, so the envelope is unwrapped when the
    payload does not carry ``choices`` at the top level. The reasoning models on
    ClinePass may also return all of their tokens as reasoning with an empty
    visible content field, in which case the reasoning text is used rather than
    discarding an otherwise usable reply.
    """
    body = payload
    if isinstance(body, dict) and "choices" not in body and "data" in body and "success" in body:
        body = body.get("data") or {}
    if not isinstance(body, dict):
        return None
    choices = body.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message") or {}
    text = message.get("content")
    if isinstance(text, list):
        text = " ".join(
            part.get("text", "") for part in text if isinstance(part, dict)
        ).strip()
    elif isinstance(text, dict):
        text = text.get("text")
    text = str(text or "").strip()
    if text:
        return text
    for key in ("reasoning_content", "reasoning"):
        fallback = str(message.get(key) or "").strip()
        if fallback:
            return fallback
    return None


def narrative_summary(changes, catalog_ids, config, now_utc=None):
    """Ask the narrative model for the weekly summary, with a documented fallback.

    The model is constrained to the supplied changes and catalog; the reply is
    capped at the configured word limit; and when every candidate fails (network,
    HTTP, or unusable reply) the deterministic summary from ``monitor.py`` is
    used. The snapshot records which produced it.
    """
    narrative = config.get("narrative") or {}
    max_words = int(narrative.get("max_words", 100))
    fallback = monitor.deterministic_summary(changes, max_words)
    candidates = [
        model_id for model_id in (narrative.get("models") or []) if model_id in set(catalog_ids)
    ]
    if not candidates:
        return {
            "summary": fallback,
            "model": None,
            "source": "deterministic",
            "note": "none of the configured narrative models is in the live catalog",
        }

    cline = config.get("cline") or {}
    api_key = os.environ.get(cline.get("api_key_env", "CLINE_API_KEY"), "")
    if not api_key:
        return {
            "summary": fallback,
            "model": None,
            "source": "deterministic",
            "note": "no Cline API key for narrative chat completions",
        }

    payload_changes = json.dumps(changes, indent=2, sort_keys=True, default=str)
    prompt = (
        "You summarize weekly changes in a model monitoring report. Use only the facts in the "
        "JSON below; never invent values; at most %d words.\n\n%s" % (max_words, payload_changes)
    )
    base = cline.get("api_base_url", "https://api.cline.bot")
    attempts = []
    for model_id in candidates:
        try:
            payload = post_json_chat(
                base + cline.get("chat_completions_path", "/api/v1/chat/completions"),
                headers=auth_headers(api_key),
                payload={
                    "model": model_id,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": narrative.get("max_tokens", 400),
                    "stream": False,
                },
                timeout=cline.get("request_timeout_seconds", 30),
                retries=cline.get("max_retries", 2),
            )
            text = extract_summary_text(payload)
            if not text:
                attempts.append("%s: empty reply" % model_id)
                continue
            return {
                "summary": monitor.truncate_words(text, max_words),
                "model": model_id,
                "source": "narrative",
            }
        except (HttpError, monitor.ApiError) as caught:
            attempts.append("%s: %s" % (model_id, caught))
    return {
        "summary": fallback,
        "model": None,
        "source": "deterministic",
        "attempts": attempts,
    }


def warsaw_now(config, now=None):
    """Current time in the report timezone (GitHub runners keep a UTC clock)."""
    tz = ZoneInfo((config.get("report") or {}).get("timezone", "Europe/Warsaw"))
    moment = now if now is not None else datetime.now(timezone.utc)
    return moment.astimezone(tz)


def should_run_scheduled(config, history_dir, now=None):
    """Decide whether a scheduled invocation may publish.

    Warsaw local time must be one of the configured weekdays (default Monday)
    and no earlier than the first configured hour, and no snapshot may exist yet
    for that date. Accepting any later hour turns GitHub's schedule delay - which
    can stretch to hours under load - into a same-day catch-up run instead of a
    silently lost report, while the same-day snapshot check still makes repeated
    cron entries mutually exclusive and keeps one report per day. Manual dispatch
    bypasses all of these checks.
    """
    report = config.get("report") or {}
    local = warsaw_now(config, now)
    hours = report.get("schedule_hours", [5])
    weekdays = report.get("schedule_weekdays", [0])
    if local.weekday() not in weekdays:
        return False, "Warsaw date %s is not a scheduled weekday" % local.date().isoformat()
    if local.hour < min(hours):
        return False, "Warsaw hour %02d is before the scheduled hours %s" % (local.hour, hours)
    if site_module.snapshot_exists(history_dir, local.date().isoformat(), monitor.MODE_LIVE):
        return False, "a live snapshot for %s already exists" % local.date().isoformat()
    if local.hour in hours:
        return True, "scheduled window matches and no snapshot exists for %s" % local.date().isoformat()
    return True, "catch-up run at Warsaw hour %02d after the scheduled hours %s with no snapshot yet for %s" % (
        local.hour,
        hours,
        local.date().isoformat(),
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def fetch_quota_context(config, user_id):
    """Optional balance/plan/usage-limit context; failures are not fatal."""
    cline = config.get("cline") or {}
    api_key = os.environ.get(cline.get("api_key_env", "CLINE_API_KEY"), "")
    if not api_key or not user_id:
        return None
    base = cline.get("api_base_url", "https://api.cline.bot")
    headers = auth_headers(api_key)
    timeout = cline.get("request_timeout_seconds", 30)
    retries = cline.get("max_retries", 2)
    context = {"user_id": user_id}
    endpoints = (
        ("balance", "/api/v1/users/%s/balance" % user_id),
        ("plan", "/api/v1/users/me/plan"),
        ("usage_limits", "/api/v1/users/me/plan/usage-limits"),
    )
    for name, path in endpoints:
        try:
            payload = http_json(base + path, headers=headers, timeout=timeout, retries=retries)
            context[name] = monitor.unwrap_envelope(payload, context=name)
        except (HttpError, monitor.ApiError):
            context[name] = None
    balance = (context.get("balance") or {}).get("balance") if isinstance(context.get("balance"), dict) else None
    credit_scale = (config.get("money_scales") or {}).get("credit_usd", 1000000)
    context["balance_usd"] = None if balance is None else balance / credit_scale
    plan = context.get("plan") or {}
    context["plan_name"] = (plan.get("plan") or {}).get("displayName") if isinstance(plan, dict) else None
    return context


def prepare_usage(rows, config):
    """Normalize billing rows and report how many were dropped as inconsistent."""
    scales = config.get("money_scales") or {"cost_usd": 100000000, "credit_usd": 1000000}
    return monitor.normalize_usage_rows(rows, scales)


def load_reference_rates():
    """The dated published reference-rate table and its provenance block."""
    reference = load_json(DATA_DIR / "clinepass_reference_rates.json")
    meta = {
        "source_name": reference.get("source_name"),
        "source_url": reference.get("source_url"),
        "retrieved_at": reference.get("retrieved_at"),
        "currency": reference.get("currency"),
        "unit": reference.get("unit"),
    }
    return reference.get("models") or {}, meta


def collect_catalog(config):
    """Discover the catalog and join optional OpenRouter metadata by slug."""
    entries, source, reason = fetch_recommended_models(config)
    openrouter = fetch_openrouter_metadata(config)
    mapping = load_json(DATA_DIR / "model_mapping.json").get("models") or {}
    metadata_by_id = {}
    for entry in entries:
        metadata, _key = monitor.match_openrouter_metadata(entry, openrouter, mapping)
        metadata_by_id[entry["id"]] = metadata or {}
    return entries, source, reason, mapping, metadata_by_id


def resolve_model_rates(entries, rows, config, reference_models, reference_meta, window):
    """Fit or fall back per model, returning rates, accepted fits, and rejections."""
    fit_config = config.get("rate_fit") or {}
    rates_by_id = {}
    fits_by_slug = {}
    rejected = {}
    for entry in entries:
        model_rows = monitor.rows_for_model(rows, entry)
        fit = None
        if not entry["free"]:
            fit = monitor.fit_rates(
                model_rows, fit_config, window.get("start"), window.get("end")
            )
            if fit.get("status") == monitor.FIT_ACCEPTED:
                fits_by_slug[entry["slug"]] = fit
            else:
                rejected[entry["id"]] = fit.get("reason")
        reference_entry = reference_models.get(entry["slug"]) or {}
        rates_by_id[entry["id"]] = monitor.resolve_rates(
            entry, fit, reference_entry, reference_meta
        )
    return rates_by_id, fits_by_slug, rejected


def collect_quality(entries, aa_index, mapping, retrieved_at):
    """Match AA evidence per model; returns the records and the match count."""
    quality_by_id = {}
    matched = 0
    for entry in entries:
        record, _key = monitor.match_aa_record(entry, aa_index, mapping)
        quality = monitor.quality_record(record, retrieved_at)
        quality_by_id[entry["id"]] = quality
        if quality:
            matched += 1
    return quality_by_id, matched


def collect_observations(entries, rows):
    """Observed blended billing rate per model, from this account's own rows."""
    observations = {}
    for entry in entries:
        observations[entry["id"]] = monitor.observed_blended_rate(
            monitor.rows_for_model(rows, entry)
        )
    return observations


def append_bounded(items, value, limit=20):
    """Append to a warning/error list without letting it grow without bound."""
    items.append(value)
    while len(items) > limit:
        items.pop(1)
    return items


FIXTURES_DIR = ROOT / "tests" / "fixtures"


def run_report(config, mode=monitor.MODE_LIVE, site_dir=None, history_dir=None, fixtures_dir=None):
    """Collect, score, compare, render, and persist one report."""
    generated = warsaw_now(config)
    day = generated.date().isoformat()
    history_path = Path(history_dir or HISTORY_DIR)
    output_dir = Path(
        site_dir or (ROOT / ("site-offline" if mode == monitor.MODE_OFFLINE else "site"))
    )
    warnings = []
    narrative_config = config.get("narrative") or {}
    max_words = int(narrative_config.get("max_words", 100))

    entries, catalog_source, catalog_reason, mapping, metadata_by_id = collect_catalog(config)
    if catalog_source == monitor.CATALOG_SOURCE_FALLBACK:
        append_bounded(
            warnings, "catalog discovery fell back to the pinned list: %s" % catalog_reason
        )
    reference_models, reference_meta = load_reference_rates()

    if mode == monitor.MODE_LIVE:
        raw_rows, usage_stats = fetch_usage_rows(config)
        quota = fetch_quota_context(config, usage_stats.get("user_id"))
    else:
        raw_rows = load_fixture_usage_rows(fixtures_dir)
        usage_stats = {"user_id": "usr-fixture", "pages": 0}
        quota = load_fixture_quota(fixtures_dir)

    rows, dropped = prepare_usage(raw_rows, config)
    window_days = int(config.get("rate_window_days", 30))
    window = {
        "days": window_days,
        "end": day,
        "start": (generated.date() - timedelta(days=window_days - 1)).isoformat(),
    }
    rows_in_window = sum(
        1
        for row in rows
        if row.get("date") and window["start"] <= row["date"] <= window["end"]
    )

    rates_by_id, fits_by_slug, rejected = resolve_model_rates(
        entries, rows, config, reference_models, reference_meta, window
    )
    scale_warning = monitor.money_scale_warning(fits_by_slug, reference_models)
    if scale_warning:
        append_bounded(warnings, scale_warning)

    if mode == monitor.MODE_LIVE:
        aa_index = fetch_aa_models(config)
        quality_by_id, matched = collect_quality(entries, aa_index, mapping, generated.isoformat())
        if matched == 0:
            raise HttpError(
                "no catalog model matched a current AA Intelligence Index; refusing to publish "
                "scores (fail closed)"
            )
    else:
        aa_index = load_fixture_aa_models(config, fixtures_dir)
        quality_by_id, matched = collect_quality(entries, aa_index, mapping, generated.isoformat())
        if matched == 0:
            append_bounded(warnings, "offline fixtures matched no AA quality score")

    observations = collect_observations(entries, rows)
    scoring = monitor.score_models(
        entries, rates_by_id, quality_by_id, metadata_by_id, observations, config
    )

    previous, previous_path = site_module.find_previous_snapshot(history_path, day, mode)
    changes = monitor.detect_changes(
        previous, {"models": scoring["models"]}, config.get("change_detection")
    )
    if mode == monitor.MODE_LIVE:
        narrative = narrative_summary(changes, [entry["id"] for entry in entries], config)
    else:
        narrative = {
            "summary": monitor.deterministic_summary(changes, max_words),
            "model": None,
            "source": "deterministic",
            "note": "offline run; no chat completion is attempted",
        }

    cline = config.get("cline") or {}
    snapshot = {
        "schema_version": 1,
        "report": {
            "generated_at": generated.isoformat(),
            "date": day,
            "weekday": generated.strftime("%A"),
            "timezone": (config.get("report") or {}).get("timezone"),
            "mode": mode,
            "title": (config.get("report") or {}).get("title"),
            "catalog": {
                "source": catalog_source,
                "reason": catalog_reason,
                "endpoint": cline.get("api_base_url", "") + cline.get("recommended_models_path", ""),
                "model_count": len(entries),
            },
            "narrative": narrative,
            "rate_window": window,
            "money_scales": config.get("money_scales"),
            "warnings": warnings,
            "previous_snapshot": None if previous_path is None else previous_path.name,
        },
        "account": quota,
        "reference_rates": reference_meta,
        "observation": {
            "rows_total": len(rows),
            "rows_dropped": dropped,
            "rows_in_window": rows_in_window,
            "pages": usage_stats.get("pages"),
            "account_id": usage_stats.get("user_id"),
            "fit_rejections": rejected,
            "aa_matches": matched,
            "catalog_ids": [entry["id"] for entry in entries],
        },
        "stats": scoring["stats"],
        "medians": scoring["medians"],
        "best_value": scoring["best_value"],
        "chart": scoring["chart"],
        "models": scoring["models"],
        "changes": changes,
        "summary": narrative["summary"],
    }
    return snapshot, output_dir, history_path


def load_fixture_usage_rows(fixtures_dir=None):
    """Concatenate the fixture usage pages used by the offline pipeline."""
    directory = Path(fixtures_dir or FIXTURES_DIR)
    rows = []
    for path in sorted(directory.glob("usage_page_*.json")):
        items, _token, _total = monitor.parse_usage_page(load_json(path), context=path.name)
        rows.extend(items)
    if not rows:
        raise HttpError("no usage fixtures found in %s" % directory)
    return rows


def load_fixture_aa_models(config, fixtures_dir=None):
    """Parse the fixture AA payload with the configured evaluation keys."""
    aa = config.get("artificial_analysis") or {}
    payload = load_json(Path(fixtures_dir or FIXTURES_DIR) / "aa_models.json")
    return monitor.parse_aa_models(
        payload,
        intelligence_key=aa.get(
            "intelligence_index_key", "artificial_analysis_intelligence_index"
        ),
        coding_key=aa.get("coding_index_key", "artificial_analysis_coding_index"),
        default_scale=aa.get("index_scale"),
    )


def load_fixture_quota(fixtures_dir=None):
    """Balance, plan, and usage-limit context from fixtures."""
    directory = Path(fixtures_dir or FIXTURES_DIR)
    context = {"user_id": "usr-fixture"}
    for name, filename in (
        ("balance", "balance.json"),
        ("plan", "plan.json"),
        ("usage_limits", "usage_limits.json"),
    ):
        path = directory / filename
        context[name] = (
            monitor.unwrap_envelope(load_json(path), context=name) if path.exists() else None
        )
    balance = (context.get("balance") or {}).get("balance")
    context["balance_usd"] = None if balance is None else balance / 1000000
    plan = context.get("plan") or {}
    context["plan_name"] = (
        (plan.get("plan") or {}).get("displayName") if isinstance(plan, dict) else None
    )
    limits = (context.get("usage_limits") or {}).get("limits")
    context["usage_limit_percent"] = (
        {item.get("type"): item.get("percentUsed") for item in limits}
        if isinstance(limits, list)
        else None
    )
    return context


def publish_report(config, snapshot, output_dir, history_path, mode):
    """Persist the snapshot, update the archive, and write the site tree."""
    snapshot_path = site_module.write_snapshot(history_path, snapshot, mode)
    report_meta = snapshot.get("report") or {}
    archive_path = Path(output_dir) / site_module.ARCHIVE_FILE
    entry = {
        "date": report_meta.get("date"),
        "generated_at": report_meta.get("generated_at"),
        "mode": mode,
        "narrative_model": (report_meta.get("narrative") or {}).get("model") or "deterministic",
        "report": "%s.html" % report_meta.get("date"),
        "snapshot": snapshot_path.name,
        "counts": monitor.change_counts(snapshot.get("changes") or {}),
    }
    limit = int((config.get("report") or {}).get("archive_limit", 26))
    entries = site_module.update_archive(archive_path, entry, limit)
    written = site_module.write_site(output_dir, snapshot, config, entries)
    return snapshot_path, archive_path, entries, written


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="python -m src.run",
        description="Build the ClinePass models comparison report.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="use fixtures only; never touches Cline, Artificial Analysis, or OpenRouter",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="run even when the schedule guard would skip this invocation",
    )
    parser.add_argument("--site-dir", default=None, help="output directory for the static site")
    parser.add_argument("--history-dir", default=None, help="output directory for snapshots")
    parser.add_argument("--fixtures-dir", default=None, help="fixture directory for --offline")
    parser.add_argument("--config", default=None, help="path to scoring_config.json")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    config = load_config(args.config)
    mode = monitor.MODE_OFFLINE if args.offline else monitor.MODE_LIVE
    history_dir = Path(args.history_dir or HISTORY_DIR)

    if mode == monitor.MODE_LIVE:
        event = os.environ.get("GITHUB_EVENT_NAME", "")
        if args.force:
            print("schedule guard bypassed by --force")
        elif event == "schedule":
            allowed, reason = should_run_scheduled(config, history_dir)
            if not allowed:
                print("SCHEDULE_SKIPPED: %s" % reason)
                return 0
            print("schedule guard: %s" % reason)

    try:
        snapshot, output_dir, history_path = run_report(
            config,
            mode=mode,
            site_dir=args.site_dir,
            history_dir=history_dir,
            fixtures_dir=args.fixtures_dir,
        )
    except (HttpError, monitor.ApiError) as error:
        print("RUN_FAILED: %s" % error, file=sys.stderr)
        return 2

    snapshot_path, _archive_path, _entries, written = publish_report(
        config, snapshot, output_dir, history_path, mode
    )
    report_meta = snapshot["report"]
    stats = snapshot["stats"]
    print(
        "mode=%s date=%s models=%d priced=%d benchmarked=%d measured=%d free=%d"
        % (
            mode,
            report_meta["date"],
            stats["discovered"],
            stats["priced"],
            stats["benchmarked"],
            stats["measured_rates"],
            stats["free_models"],
        )
    )
    print(
        "summary source=%s model=%s"
        % (report_meta["narrative"].get("source"), report_meta["narrative"].get("model"))
    )
    for warning in report_meta.get("warnings") or []:
        print("WARNING: %s" % warning)
    print("snapshot: %s" % snapshot_path)
    print("site: %s (%d files)" % (output_dir, len(written)))
    if mode == monitor.MODE_OFFLINE:
        print("OFFLINE RUN: fixtures only; this report is not a live evaluation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())