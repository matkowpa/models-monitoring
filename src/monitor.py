"""Pure parsing, rate fitting, and scoring for the ClinePass models report.

Nothing in this module touches the network or the file system: ``src/run.py``
owns the external calls and ``src/site.py`` owns rendering. Keeping this module
pure is what makes the offline test suite meaningful - every decision below is
exercised without credentials, which is the boundary models_monitoring.md
section 7 asks for.
"""

from __future__ import annotations

import math
import re
import statistics
from datetime import date, datetime, timedelta

CATALOG_SOURCE_LIVE = "recommended-models"
CATALOG_SOURCE_FALLBACK = "fallback"

PROVENANCE_MEASURED = "measured"
PROVENANCE_REFERENCE = "reference"
PROVENANCE_CATALOG_FREE = "catalog-free"
PROVENANCE_INPUT_FALLBACK = "input-fallback"

RATE_CLASSES = ("input", "output", "cache_read", "cache_write")
FIT_CLASSES = ("input", "output", "cache_read")
BILLING_TYPE_CLINEPASS = "cline-pass"

MODE_LIVE = "live"
MODE_OFFLINE = "offline"

FIT_ACCEPTED = "measured"
FIT_REJECTED = "rejected"


class ApiError(RuntimeError):
    """A response that is not a usable success envelope."""


def as_float(value):
    """Return a finite float, or None for anything else."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def as_int(value):
    number = as_float(value)
    return None if number is None else int(number)


def clamp(value, low, high):
    return max(low, min(high, value))


def normalize_key(value):
    """Fold a model name or slug into a comparison key."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def model_slug(model_id):
    """Catalog slug for a model id.

    ``cline-pass/<slug>`` and ``cline-free/<slug>`` drop their provider prefix;
    provider-style free ids such as ``stealth/space-bunny-alpha`` are kept whole
    so two labs cannot collide on the same short name.
    """
    model_id = str(model_id or "")
    for prefix in ("cline-pass/", "cline-free/", "clinepass/"):
        if model_id.startswith(prefix):
            return model_id[len(prefix):]
    return model_id


def unwrap_envelope(payload, context="request"):
    """Return ``data`` from a Cline ``{ data, error, success }`` envelope.

    ``success: false`` and ``data: null`` are failures, not empty results, so
    they raise instead of quietly producing zero rows.
    """
    if not isinstance(payload, dict):
        raise ApiError(f"{context}: expected an object envelope")
    if payload.get("success") is False:
        raise ApiError(f"{context}: {payload.get('error') or 'request failed'}")
    if "data" not in payload:
        raise ApiError(f"{context}: envelope has no data field")
    data = payload["data"]
    if data is None:
        raise ApiError(f"{context}: {payload.get('error') or 'empty data'}")
    return data


def parse_recommended_models(payload):
    """Parse ``/api/v1/ai/cline/recommended-models``.

    The payload carries only id, name, and description - no prices and no
    context windows - so catalog discovery can never be mistaken for pricing.
    Free-model ids are not uniformly prefixed, so membership in the ``free``
    array is the discriminator, not the id prefix.
    """
    if not isinstance(payload, dict):
        raise ApiError("recommended-models: expected an object payload")
    entries = []
    seen = set()
    for field, is_free in (("clinePass", False), ("free", True)):
        raw = payload.get(field)
        if raw is None:
            continue
        if not isinstance(raw, list):
            raise ApiError(f"recommended-models: {field} must be a list")
        for entry in raw:
            if isinstance(entry, str):
                entry = {"id": entry}
            if not isinstance(entry, dict):
                continue
            model_id = str(entry.get("id") or "").strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            declared_free = entry.get("free")
            entry_is_free = is_free if not isinstance(declared_free, bool) else declared_free
            entries.append(
                {
                    "id": model_id,
                    "slug": model_slug(model_id),
                    "name": str(entry.get("name") or "").strip() or model_id,
                    "description": str(entry.get("description") or "").strip() or None,
                    "free": entry_is_free,
                }
            )
    return entries


def has_paid_models(entries):
    """True when discovery returned at least one non-free ClinePass model."""
    return any(not entry["free"] for entry in entries)


def parse_catalog_fallback(payload):
    """Parse the pinned ``data/clinepass_catalog_fallback.json`` list."""
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise ApiError("catalog fallback: expected a models list")
    normalised = []
    for item in models:
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        entry.setdefault("free", str(entry.get("id", "")).startswith("cline-free/"))
        normalised.append(entry)
    return parse_recommended_models({"clinePass": normalised})


def parse_openrouter_models(payload):
    """Index OpenRouter metadata by every plausible name for a model.

    Only display name and context window are taken: OpenRouter prices are never
    substituted for Cline rates.
    """
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        raise ApiError("openrouter: expected a data list")
    index = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "")
        context_window = as_int((item.get("top_provider") or {}).get("context_length"))
        if context_window is None:
            context_window = as_int(item.get("context_length"))
        metadata = {
            "openrouter_id": model_id or None,
            "name": str(item.get("name") or "").strip() or None,
            "context_window": context_window,
        }
        keys = {normalize_key(model_id), normalize_key(model_id.split("/")[-1])}
        keys.discard("")
        for key in keys:
            index.setdefault(key, metadata)
    return index


def match_openrouter_metadata(entry, openrouter_index, mapping):
    """Return (metadata, matched_key) for a catalog entry, or (None, None)."""
    if not openrouter_index:
        return None, None
    mapped = (mapping or {}).get(entry["slug"]) or {}
    for candidate in (entry["id"], entry["slug"], mapped.get("openrouter_slug")):
        key = normalize_key(candidate)
        if key and key in openrouter_index:
            return openrouter_index[key], candidate
    return None, None


def _aa_items(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "models"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise ApiError("artificial analysis: expected a list of models")


def parse_aa_models(payload, intelligence_key, coding_key=None, default_scale=None):
    """Index Artificial Analysis models by every plausible name for a model.

    A non-numeric or absent evaluation stays absent so the caller can fail
    closed instead of inventing a score, and a key that resolves to more than
    one AA model is recorded as ambiguous (``None``) so the model is published
    without a score rather than with a guessed one.
    """
    index = {}
    ambiguous = set()
    for rank, item in enumerate(_aa_items(payload)):
        if not isinstance(item, dict):
            continue
        evaluations = item.get("evaluations")
        if not isinstance(evaluations, dict):
            evaluations = {}
        scale = item.get("intelligence_index_scale") or default_scale or {}
        record = {
            "name": str(item.get("name") or "").strip() or None,
            "aa_slug": str(item.get("slug") or item.get("id") or "").strip() or None,
            "intelligence_index": as_float(evaluations.get(intelligence_key)),
            "coding_index": as_float(evaluations.get(coding_key)) if coding_key else None,
            "index_scale": {
                "min": as_float(scale.get("min")),
                "max": as_float(scale.get("max")),
            },
            "order": rank,
        }
        keys = {normalize_key(record["aa_slug"]), normalize_key(record["name"])}
        keys.discard("")
        for key in keys:
            if key in index:
                ambiguous.add(key)
            index.setdefault(key, record)
    for key in ambiguous:
        index[key] = None
    return index


def match_aa_record(entry, aa_index, mapping):
    """Return ``(aa_record, matched_key)`` for a catalog entry, or ``(None, None)``.

    Candidates come from the explicit mapping first, then from the catalog's own
    id, slug, and display name. An ambiguous key yields no match.
    """
    if not aa_index:
        return None, None
    mapped = (mapping or {}).get(entry["slug"]) or {}
    candidates = [
        mapped.get("aa_slug"),
        mapped.get("aa_name"),
        entry["slug"],
        entry["id"],
        entry["name"],
    ]
    candidates.extend(mapped.get("aliases") or [])
    seen = set()
    for candidate in candidates:
        key = normalize_key(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        if aa_index.get(key):
            return aa_index[key], candidate
    return None, None


def normalize_quality(score, scale=None):
    """Normalize a declared-scale score onto 0-100.

    The active feed publishes the Intelligence Index on 0-100, so the normalized
    value is effectively the published score; a different declared scale is
    normalized using its bounds.
    """
    score = as_float(score)
    if score is None:
        return None
    low = as_float((scale or {}).get("min"))
    high = as_float((scale or {}).get("max"))
    low = 0.0 if low is None else low
    high = 100.0 if high is None else high
    if high <= low:
        return None
    return clamp(100.0 * (score - low) / (high - low), 0.0, 100.0)


def quality_record(record, retrieved_at):
    """Quality evidence for one model, or ``None`` when there is no current score."""
    if not record:
        return None
    intelligence = normalize_quality(record.get("intelligence_index"), record.get("index_scale"))
    if intelligence is None:
        return None
    coding = as_float(record.get("coding_index"))
    return {
        "intelligence_index": round(intelligence, 2),
        "coding_index": None if coding is None else round(coding, 2),
        "coding_index_display_only": True,
        "aa_name": record.get("name"),
        "aa_slug": record.get("aa_slug"),
        "retrieved_at": retrieved_at,
    }


# ---------------------------------------------------------------------------
# Billing usage ingestion
# ---------------------------------------------------------------------------


def parse_usage_page(payload, context="usages"):
    """Return ``(items, next_token, total)`` from a paged usage response."""
    data = unwrap_envelope(payload, context=context)
    if not isinstance(data, dict):
        raise ApiError(f"{context}: expected a data object")
    items = data.get("items")
    if not isinstance(items, list):
        raise ApiError(f"{context}: data has no items list")
    return items, data.get("nextToken") or None, as_int(data.get("total"))


def parse_daily_usage(payload, context="usages/daily"):
    """Return server-aggregated daily usage rows."""
    data = unwrap_envelope(payload, context=context)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    raise ApiError(f"{context}: data has no items list")


def daily_windows(start, end, max_days=31):
    """Split an inclusive date range into windows the daily endpoint accepts."""
    if isinstance(start, str):
        start = date.fromisoformat(start)
    if isinstance(end, str):
        end = date.fromisoformat(end)
    if max_days < 1:
        raise ValueError("max_days must be at least 1")
    windows = []
    cursor = start
    while cursor <= end:
        last = min(cursor + timedelta(days=max_days - 1), end)
        windows.append((cursor.isoformat(), last.isoformat()))
        cursor = last + timedelta(days=1)
    return windows


def normalize_usage_row(row, money_scales):
    """Normalize one usage row, or ``None`` when the row must be dropped.

    Inconsistent rows (cached tokens above prompt tokens, negative counts) are
    dropped rather than repaired. The raw integer money fields are kept next to
    the normalized values so the scale assumption stays auditable.
    """
    if not isinstance(row, dict):
        return None
    prompt = as_float(row.get("promptTokens"))
    completion = as_float(row.get("completionTokens"))
    cached = as_float(row.get("cachedTokens"))
    prompt = 0.0 if prompt is None else prompt
    completion = 0.0 if completion is None else completion
    cached = 0.0 if cached is None else cached
    if prompt < 0 or completion < 0 or cached < 0:
        return None
    if cached > prompt:
        return None
    created = str(row.get("createdAt") or "")
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    total = as_float(row.get("totalTokens"))
    if total is None:
        total = prompt + completion
    cost_raw = as_float(row.get("costUsd"))
    credits_raw = as_float(row.get("creditsUsed"))
    return {
        "id": row.get("id"),
        "created_at": created or None,
        "date": created[:10] or None,
        "billing_type": (
            BILLING_TYPE_CLINEPASS
            if row.get("aiModelTypeName") == BILLING_TYPE_CLINEPASS
            else "cline-usage"
        ),
        "raw_model_type_name": row.get("aiModelTypeName"),
        "provider": row.get("aiInferenceProviderName") or "unknown",
        "model": str(row.get("aiModelName") or ""),
        "raw_model": str(metadata.get("raw_model") or row.get("aiModelName") or ""),
        "model_id": metadata.get("model_id"),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "fresh_input_tokens": prompt - cached,
        "total_tokens": total,
        "cost_usd_raw": cost_raw,
        "cost_usd": None if cost_raw is None else cost_raw / money_scales["cost_usd"],
        "credits_usd_raw": credits_raw,
        "credits_usd": None if credits_raw is None else credits_raw / money_scales["credit_usd"],
    }


def normalize_usage_rows(rows, money_scales):
    """Normalize a batch, reporting how many rows were dropped."""
    normalized = []
    dropped = 0
    for row in rows or []:
        item = normalize_usage_row(row, money_scales)
        if item is None:
            dropped += 1
            continue
        normalized.append(item)
    return normalized, dropped


def rows_for_model(rows, entry):
    """ClinePass usage rows that belong to one catalog entry.

    Matching uses the row's model name and raw model id against the entry slug and
    id. Only rows billed as ClinePass are considered, which also keeps a free twin
    id (``cline-free/<slug>``) from contaminating the paid model's fit.
    """
    wanted = {normalize_key(entry["slug"]), normalize_key(entry["id"])}
    wanted.discard("")
    matched = []
    for row in rows or []:
        if row.get("billing_type") != BILLING_TYPE_CLINEPASS:
            continue
        keys = {normalize_key(row.get("model")), normalize_key(row.get("raw_model"))}
        keys.discard("")
        if keys & wanted:
            matched.append(row)
    return matched


# ---------------------------------------------------------------------------
# Rate fitting (models_monitoring.md section 3.2)
# ---------------------------------------------------------------------------


def solve_symmetric(matrix, vector):
    """Solve a small dense system with partial pivoting, or return ``None``."""
    size = len(vector)
    work = [list(row) + [vector[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(work[row][column]))
        if abs(work[pivot][column]) < 1e-12:
            return None
        work[column], work[pivot] = work[pivot], work[column]
        divisor = work[column][column]
        work[column] = [value / divisor for value in work[column]]
        for row in range(size):
            if row == column:
                continue
            factor = work[row][column]
            if factor:
                work[row] = [
                    value - factor * other for value, other in zip(work[row], work[column])
                ]
    return [work[index][size] for index in range(size)]


def symmetric_eigenvalues(matrix, sweeps=60):
    """Eigenvalues of a small symmetric matrix, via Jacobi rotations."""
    size = len(matrix)
    work = [list(row) for row in matrix]
    for _ in range(sweeps):
        largest = (0.0, 0, 0)
        for i in range(size):
            for j in range(i + 1, size):
                if abs(work[i][j]) > largest[0]:
                    largest = (abs(work[i][j]), i, j)
        if largest[0] < 1e-12:
            break
        _, p, q = largest
        if abs(work[p][p] - work[q][q]) < 1e-15:
            angle = math.pi / 4
        else:
            angle = 0.5 * math.atan2(2 * work[p][q], work[p][p] - work[q][q])
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        for k in range(size):
            a_kp, a_kq = work[k][p], work[k][q]
            work[k][p] = cos_a * a_kp + sin_a * a_kq
            work[k][q] = -sin_a * a_kp + cos_a * a_kq
        for k in range(size):
            a_pk, a_qk = work[p][k], work[q][k]
            work[p][k] = cos_a * a_pk + sin_a * a_qk
            work[q][k] = -sin_a * a_pk + cos_a * a_qk
    return sorted(work[index][index] for index in range(size))


def nnls_small(gram, rhs):
    """Exact non-negative least squares for a small system.

    Every active set is solved and checked against the KKT conditions, which is
    exact for three unknowns. Non-negativity is therefore a real constraint
    rather than a negative fit being clamped to zero after the fact.
    """
    size = len(rhs)
    total = sum(value * value for value in rhs)
    best = None
    for mask in range(1 << size):
        active = [index for index in range(size) if mask & (1 << index)]
        solution = []
        if active:
            sub_matrix = [[gram[i][j] for j in active] for i in active]
            sub_vector = [rhs[i] for i in active]
            solution = solve_symmetric(sub_matrix, sub_vector)
            if solution is None or any(value < 0 for value in solution):
                continue
        full = [0.0] * size
        for index, value in zip(active, solution):
            full[index] = value
        gradient = [sum(gram[i][j] * full[j] for j in range(size)) - rhs[i] for i in range(size)]
        if any(gradient[index] < -1e-9 for index in range(size) if index not in active):
            continue
        residual = total - 2 * sum(full[i] * rhs[i] for i in range(size))
        residual += sum(full[i] * gram[i][j] * full[j] for i in range(size) for j in range(size))
        if best is None or residual < best["residual"]:
            best = {"solution": full, "residual": residual}
    return best


def empty_fit(window_start=None, window_end=None, reason=None):
    """A rejected fit record, so callers never have to test for missing keys."""
    return {
        "status": FIT_REJECTED,
        "reason": reason,
        "rates": {rate_class: None for rate_class in FIT_CLASSES},
        "rows": 0,
        "rows_in_window": 0,
        "dropped_rows": 0,
        "window_start": window_start,
        "window_end": window_end,
        "condition_number": None,
        "rms_residual_usd": None,
        "allowed_rms_residual_usd": None,
        "total_reconciliation_error": None,
    }


def fit_rates(rows, fit_config, window_start=None, window_end=None):
    """Fit input, output, and cache-read rates for one model.

    ClinePass bills a single reference cost per request, so the per-class rates
    are recovered jointly by non-negative least squares from the token classes in
    each row. The fit is accepted only when the token mixes identify three rates,
    the rates are positive, the model total reconciles, and the per-row residual
    stays inside tolerance; otherwise the caller falls back to the dated
    reference table.
    """
    in_window = [
        row
        for row in rows or []
        if (not window_start or (row.get("date") and row["date"] >= window_start))
        and (not window_end or (row.get("date") and row["date"] <= window_end))
    ]
    usable = []
    dropped = 0
    for row in in_window:
        if not row.get("cost_usd") or not row.get("total_tokens"):
            dropped += 1
            continue
        usable.append(row)

    result = empty_fit(window_start, window_end)
    result["rows"] = len(usable)
    result["rows_in_window"] = len(in_window)
    result["dropped_rows"] = dropped

    minimum = int(fit_config.get("min_rows", 12))
    if len(usable) < minimum:
        result["reason"] = (
            f"{len(usable)} usable ClinePass rows in the window, minimum is {minimum}"
        )
        return result

    token_keys = {
        "input": "fresh_input_tokens",
        "output": "completion_tokens",
        "cache_read": "cached_tokens",
    }
    row_count = len(usable)
    columns = [
        [row[token_keys[rate_class]] / 1_000_000.0 for row in usable] for rate_class in FIT_CLASSES
    ]
    target = [row["cost_usd"] for row in usable]
    size = len(FIT_CLASSES)

    norms = [math.sqrt(sum(value * value for value in column)) or 1.0 for column in columns]
    scaled = [[value / norms[index] for value in column] for index, column in enumerate(columns)]
    gram = [
        [sum(scaled[i][k] * scaled[j][k] for k in range(row_count)) for j in range(size)]
        for i in range(size)
    ]
    rhs = [sum(scaled[i][k] * target[k] for k in range(row_count)) for i in range(size)]

    eigenvalues = symmetric_eigenvalues(gram)
    smallest, largest = eigenvalues[0], eigenvalues[-1]
    condition = float("inf") if smallest <= 0 else math.sqrt(largest / smallest)
    result["condition_number"] = None if math.isinf(condition) else round(condition, 3)
    if condition > float(fit_config.get("max_condition_number", 1e6)):
        result["reason"] = (
            "token-mix matrix is rank deficient or ill-conditioned "
            f"(condition number {condition:.3g})"
        )
        return result

    solved = nnls_small(gram, rhs)
    if not solved:
        result["reason"] = "no non-negative solution for the token mixes in this window"
        return result

    fitted = {
        rate_class: solved["solution"][index] / norms[index]
        for index, rate_class in enumerate(FIT_CLASSES)
    }
    predicted = [
        sum(
            fitted[rate_class] * columns[index][k]
            for index, rate_class in enumerate(FIT_CLASSES)
        )
        for k in range(row_count)
    ]
    residuals = [predicted[k] - target[k] for k in range(row_count)]
    rms = math.sqrt(sum(value * value for value in residuals) / row_count)
    total_target = sum(target)
    reconciliation = abs(sum(predicted) - total_target) / total_target if total_target else None
    mean_cost = total_target / row_count
    allowed_rms = max(
        float(fit_config.get("rms_residual_ratio", 0.02)) * mean_cost,
        float(fit_config.get("rms_residual_floor_usd", 0.00002)),
    )

    result["rates"] = {rate_class: round(fitted[rate_class], 6) for rate_class in FIT_CLASSES}
    result["rms_residual_usd"] = round(rms, 8)
    result["allowed_rms_residual_usd"] = round(allowed_rms, 8)
    result["total_reconciliation_error"] = (
        None if reconciliation is None else round(reconciliation, 6)
    )

    if fitted["input"] <= 0 or fitted["output"] <= 0:
        result["reason"] = "fitted base input or output rate is not strictly positive"
        return result
    maximum_reconciliation = float(fit_config.get("max_total_reconciliation_error", 0.01))
    if reconciliation is None or reconciliation > maximum_reconciliation:
        result["reason"] = (
            f"total reconciliation error {reconciliation} exceeds {maximum_reconciliation}"
        )
        return result
    if rms > allowed_rms:
        result["reason"] = (
            f"per-row RMS residual {rms:.6g} USD exceeds the allowed {allowed_rms:.6g} USD"
        )
        return result

    result["status"] = FIT_ACCEPTED
    result["reason"] = None
    return result


def resolve_rates(entry, fit, reference_entry=None, reference_meta=None):
    """Per-class rates with provenance for one catalog entry.

    A catalog-free entry is $0 by declaration. Otherwise an accepted fit supplies
    input, output, and cache read, and a rejected or absent fit falls back to the
    dated reference table for those three classes at once - measured and
    reference classes are never mixed inside one model. Cache write is never
    fitted: it comes from the reference table when that defines it, and from the
    input rate otherwise, which the provenance field records.
    """
    reference_entry = reference_entry or {}
    reference_meta = reference_meta or {}
    record = {
        **{
            rate_class: {"usd_per_mtok": None, "provenance": None}
            for rate_class in RATE_CLASSES
        },
        "catalog_free": bool(entry.get("free")),
        "fit": fit,
        "reference_source": {
            "source_name": reference_meta.get("source_name"),
            "source_url": reference_meta.get("source_url"),
            "retrieved_at": reference_meta.get("retrieved_at"),
        },
    }

    if entry.get("free"):
        for rate_class in RATE_CLASSES:
            record[rate_class] = {"usd_per_mtok": 0.0, "provenance": PROVENANCE_CATALOG_FREE}
        return record

    if fit and fit.get("status") == FIT_ACCEPTED:
        for rate_class in FIT_CLASSES:
            record[rate_class] = {
                "usd_per_mtok": fit["rates"].get(rate_class),
                "provenance": PROVENANCE_MEASURED,
                "window_start": fit.get("window_start"),
                "window_end": fit.get("window_end"),
                "rows": fit.get("rows"),
                "rms_residual_usd": fit.get("rms_residual_usd"),
                "condition_number": fit.get("condition_number"),
            }
    else:
        for rate_class in FIT_CLASSES:
            value = as_float(reference_entry.get(rate_class))
            if value is None:
                continue
            record[rate_class] = {
                "usd_per_mtok": value,
                "provenance": PROVENANCE_REFERENCE,
                "source_url": reference_meta.get("source_url"),
                "retrieved_at": reference_meta.get("retrieved_at"),
            }

    input_rate = record["input"]["usd_per_mtok"]
    if input_rate is not None:
        cache_write = as_float(reference_entry.get("cache_write"))
        if cache_write is not None:
            record["cache_write"] = {
                "usd_per_mtok": cache_write,
                "provenance": PROVENANCE_REFERENCE,
                "source_url": reference_meta.get("source_url"),
                "retrieved_at": reference_meta.get("retrieved_at"),
            }
        else:
            record["cache_write"] = {
                "usd_per_mtok": input_rate,
                "provenance": PROVENANCE_INPUT_FALLBACK,
                "note": (
                    "usage rows do not report cache-write tokens and the reference table has "
                    "no value, so cache write is charged at the input rate"
                ),
            }
    return record


def money_scale_warning(fits_by_slug, reference_models):
    """Flag an implausible measured/reference rate ratio.

    The integer money scales are inferred from an audited third-party client
    rather than from public API documentation, and a wrong scale is off by a
    large factor. A median ratio inside 0.2-5x means the scales are plausible;
    otherwise the snapshot carries a warning instead of publishing a wrong price.
    """
    ratios = []
    for slug, fit in (fits_by_slug or {}).items():
        if not fit or fit.get("status") != FIT_ACCEPTED:
            continue
        reference = (reference_models or {}).get(slug) or {}
        for rate_class in FIT_CLASSES:
            measured = as_float(fit["rates"].get(rate_class))
            published = as_float(reference.get(rate_class))
            if measured and published and measured > 0 and published > 0:
                ratios.append(measured / published)
    if not ratios:
        return None
    ratio = statistics.median(ratios)
    if 0.2 <= ratio <= 5.0:
        return None
    return (
        f"median measured/reference rate ratio is {ratio:.3g}, outside 0.2-5.0; re-verify the "
        "costUsd and creditsUsed money scales against a known request before trusting these rates"
    )


def observed_blended_rate(rows):
    """What this account actually paid per million tokens, from billing rows."""
    cost = 0.0
    tokens = 0.0
    counted = 0
    for row in rows or []:
        if row.get("cost_usd") is None or not row.get("total_tokens"):
            continue
        cost += row["cost_usd"]
        tokens += row["total_tokens"]
        counted += 1
    if not tokens or not counted:
        return None, 0
    return cost / (tokens / 1_000_000.0), counted


# ---------------------------------------------------------------------------
# Scoring (models_monitoring.md sections 3.2 - 3.4)
# ---------------------------------------------------------------------------


def rate_value(rates, rate_class):
    entry = (rates or {}).get(rate_class) or {}
    return as_float(entry.get("usd_per_mtok"))


def rate_provenance(rates, rate_class):
    entry = (rates or {}).get(rate_class) or {}
    return entry.get("provenance")


def base_rates_usable(rates):
    """True when a task cost can be computed from this model's rates."""
    input_rate = rate_value(rates, "input")
    output_rate = rate_value(rates, "output")
    if input_rate is None or output_rate is None:
        return False
    if (rates or {}).get("catalog_free"):
        return True
    return input_rate > 0 and output_rate > 0


def task_cost(rates, profile, verbosity=1.0):
    """Estimated USD cost of one task profile.

    Missing cache rates fall back to the input rate, while an explicitly reported
    zero cache rate means free cache tokens. Missing or zero base input/output
    rates make the cost unavailable - except for a catalog-free model, where zero
    is a declared promotion rather than missing data.
    """
    if not rates or not profile or not base_rates_usable(rates):
        return None
    input_rate = rate_value(rates, "input")
    output_rate = rate_value(rates, "output")
    cache_read = rate_value(rates, "cache_read")
    cache_write = rate_value(rates, "cache_write")
    if cache_read is None:
        cache_read = input_rate
    if cache_write is None:
        cache_write = input_rate
    weighted = (
        profile["cache_read_tokens"] * cache_read
        + profile["fresh_input_tokens"] * input_rate
        + profile["cache_write_tokens"] * cache_write
        + profile["output_tokens"] * output_rate
    )
    multiplier = as_float(verbosity)
    multiplier = 1.0 if multiplier is None else multiplier
    return weighted / 1_000_000.0 * multiplier


def price_median(costs):
    """Median of the positive costs; the quality score never contributes."""
    values = sorted(value for value in costs if value is not None and value > 0)
    if not values:
        return None
    return statistics.median(values)


def efficiency(quality, cost, median, penalty):
    """Efficiency points, or ``None`` when any component is unavailable.

    Zero-cost (catalog-free) models get no efficiency value: the formula is
    logarithmic, so a zero cost has no place in it. They are reported as free
    instead of as infinitely efficient.
    """
    if quality is None or cost is None or median is None:
        return None
    if cost <= 0 or median <= 0:
        return None
    multiplier = as_float(penalty)
    if multiplier is None:
        return None
    return quality - multiplier * math.log2(cost / median)


def pareto_members(points):
    """Non-dominated member ids for a set of chart or profile points.

    A model is dominated when another model has equal-or-higher quality and
    equal-or-lower cost with at least one strict improvement. Zero-cost models
    stay in the set because nothing can be cheaper.
    """
    flags = {}
    for point in points:
        dominated = False
        if point["quality"] is not None and point["cost"] is not None:
            for other in points:
                if other is point:
                    continue
                if other["quality"] is None or other["cost"] is None:
                    continue
                if other["quality"] >= point["quality"] and other["cost"] <= point["cost"]:
                    if other["quality"] > point["quality"] or other["cost"] < point["cost"]:
                        dominated = True
                        break
        flags[point["id"]] = not dominated
    return flags


def blended_input_output(rates):
    """Informational 0.8/0.2 blended rate, used for price-change detection only."""
    input_rate = rate_value(rates, "input")
    output_rate = rate_value(rates, "output")
    if input_rate is None or output_rate is None:
        return None
    return 0.8 * input_rate + 0.2 * output_rate


def family_of(entry):
    """Family label for the table filter (first alphabetic run of the slug)."""
    match = re.match(r"[a-z]+", str(entry.get("slug") or "").lower())
    return match.group(0) if match else str(entry.get("slug") or "")


def score_models(entries, rates_by_id, quality_by_id, metadata_by_id, observations_by_id, config):
    """Assemble per-model records, medians, best values, and chart data.

    Cost, efficiency, Pareto membership, and best-value selection follow section 3
    of models_monitoring.md: separate profiles, no quality gate, and catalog-free
    models excluded from the efficiency formula while staying in the Pareto set.
    """
    profiles = config.get("profiles") or {}
    verbosity = config.get("verbosity_multipliers") or {}
    records = []
    for entry in entries:
        model_id = entry["id"]
        rates = rates_by_id.get(model_id) or {}
        metadata = metadata_by_id.get(model_id) or {}
        observed, observed_rows = observations_by_id.get(model_id, (None, 0))
        records.append(
            {
                "id": model_id,
                "slug": entry["slug"],
                "name": metadata.get("name") or entry["name"],
                "catalog_name": entry["name"],
                "description": entry.get("description"),
                "free": bool(entry.get("free")),
                "family": family_of(entry),
                "context_window": metadata.get("context_window"),
                "openrouter_id": metadata.get("openrouter_id"),
                "rates": rates,
                "quality": quality_by_id.get(model_id),
                "cost_usd": {
                    name: task_cost(rates, profile, verbosity.get(model_id, 1.0))
                    for name, profile in profiles.items()
                },
                "efficiency": {name: None for name in profiles},
                "pareto": {name: False for name in profiles},
                "blended_input_output_usd_per_mtok": blended_input_output(rates),
                "observed_blended_usd_per_mtok": None if observed is None else round(observed, 6),
                "observed_rows": observed_rows,
                "priced": base_rates_usable(rates),
            }
        )

    medians = {
        name: price_median(record["cost_usd"].get(name) for record in records) for name in profiles
    }

    membership = {}
    for name, profile in profiles.items():
        for record in records:
            record["efficiency"][name] = efficiency(
                (record["quality"] or {}).get("intelligence_index"),
                record["cost_usd"].get(name),
                medians.get(name),
                profile.get("penalty"),
            )
        points = [
            {
                "id": record["id"],
                "quality": (record["quality"] or {}).get("intelligence_index"),
                "cost": record["cost_usd"].get(name),
            }
            for record in records
            if record["quality"] and record["cost_usd"].get(name) is not None
        ]
        membership[name] = pareto_members(points)
        for record in records:
            record["pareto"][name] = bool(membership[name].get(record["id"]))

    best_value = {}
    for name in profiles:
        candidates = [
            record
            for record in records
            if record["pareto"][name] and record["efficiency"].get(name) is not None
        ]
        candidates.sort(key=lambda record: (-record["efficiency"][name], record["cost_usd"][name]))
        if not candidates:
            best_value[name] = None
            continue
        winner = candidates[0]
        best_value[name] = {
            "id": winner["id"],
            "name": winner["name"],
            "efficiency": round(winner["efficiency"][name], 2),
            "cost_usd": round(winner["cost_usd"][name], 6),
            "quality": (winner["quality"] or {}).get("intelligence_index"),
        }

    return _with_chart(records, profiles, medians, best_value)


def _with_chart(records, profiles, medians, best_value):
    """Chart points, the single frontier, the statistics block, and the report."""
    plotted = []
    free_band = []
    for record in records:
        quality = (record["quality"] or {}).get("intelligence_index")
        if quality is None:
            continue
        available = {name: record["cost_usd"].get(name) for name in profiles}
        positive = [value for value in available.values() if value is not None and value > 0]
        point = {
            "id": record["id"],
            "name": record["name"],
            "quality": quality,
            "free": record["free"],
            "profiles": sorted(name for name, value in available.items() if value is not None),
        }
        if not positive:
            if point["profiles"]:
                point["cost"] = 0.0
                free_band.append(point)
            continue
        point["cost"] = sum(positive) / len(positive)
        plotted.append(point)

    frontier = pareto_members(plotted + free_band)
    for point in plotted + free_band:
        point["frontier"] = bool(frontier.get(point["id"]))

    benchmarked = [record for record in records if record["quality"]]
    highest = None
    if benchmarked:
        top = max(benchmarked, key=lambda record: record["quality"]["intelligence_index"])
        highest = {
            "id": top["id"],
            "name": top["name"],
            "quality": top["quality"]["intelligence_index"],
        }

    costed = [
        (record, min(value for value in record["cost_usd"].values() if value is not None))
        for record in records
        if any(value is not None for value in record["cost_usd"].values())
    ]
    cheapest = None
    if costed:
        record, cost = min(costed, key=lambda pair: pair[1])
        cheapest = {
            "id": record["id"],
            "name": record["name"],
            "cost_usd": round(cost, 6),
            "free": record["free"],
        }

    measured = [
        record
        for record in records
        if any(rate_provenance(record["rates"], cls) == PROVENANCE_MEASURED for cls in FIT_CLASSES)
    ]

    return {
        "models": records,
        "medians": medians,
        "best_value": best_value,
        "chart": {"points": plotted, "free_band": free_band},
        "stats": {
            "discovered": len(records),
            "priced": sum(1 for record in records if record["priced"]),
            "benchmarked": len(benchmarked),
            "measured_rates": len(measured),
            "free_models": sum(1 for record in records if record["free"]),
            "highest_quality": highest,
            "lowest_cost": cheapest,
        },
    }


# ---------------------------------------------------------------------------
# Change detection (models_monitoring.md section 4)
# ---------------------------------------------------------------------------


def models_by_id(models):
    """Index model records by id."""
    return {model["id"]: model for model in models or []}


def relative_change(previous, current):
    """Signed relative change, or ``None`` when it cannot be computed."""
    if previous in (None, 0) or current is None:
        return None
    return (current - previous) / abs(previous)


def detect_changes(previous, current, change_config=None):
    """Compare two snapshots using only the supported change classes.

    Catalog additions and removals, AA quality changes, per-class rate changes,
    and observed blended-rate drift. Efficiency rank, Pareto membership, and
    recommendation shifts are deliberately out of scope until they are specified
    and tested.
    """
    change_config = change_config or {}
    rate_threshold = float(change_config.get("material_rate_change_ratio", 0.01))
    blend_threshold = float(change_config.get("observed_blend_change_ratio", 0.1))

    before = models_by_id((previous or {}).get("models"))
    after = models_by_id((current or {}).get("models"))
    changes = {
        "has_previous": bool(before),
        "first_run": not before,
        "catalog_added": [],
        "catalog_removed": [],
        "quality_changed": {},
        "rate_changed": {},
        "observed_blend_changed": {},
    }
    if not before:
        return changes

    changes["catalog_added"] = sorted(set(after) - set(before))
    changes["catalog_removed"] = sorted(set(before) - set(after))

    for model_id in sorted(set(before) & set(after)):
        old, new = before[model_id], after[model_id]
        old_quality = (old.get("quality") or {}).get("intelligence_index")
        new_quality = (new.get("quality") or {}).get("intelligence_index")
        if old_quality != new_quality and (old_quality is not None or new_quality is not None):
            changes["quality_changed"][model_id] = {
                "name": new.get("name"),
                "from": old_quality,
                "to": new_quality,
            }

        per_class = {}
        for rate_class in RATE_CLASSES:
            old_rate = rate_value(old.get("rates"), rate_class)
            new_rate = rate_value(new.get("rates"), rate_class)
            if old_rate is None or new_rate is None or old_rate == new_rate:
                continue
            ratio = relative_change(old_rate, new_rate)
            if ratio is None or abs(ratio) < rate_threshold:
                continue
            entry = {"from": old_rate, "to": new_rate, "change_ratio": round(ratio, 6)}
            old_provenance = rate_provenance(old.get("rates"), rate_class)
            new_provenance = rate_provenance(new.get("rates"), rate_class)
            if old_provenance != new_provenance:
                entry["provenance_change"] = {"from": old_provenance, "to": new_provenance}
                entry["note"] = "provenance changed; these two values are not directly comparable"
            per_class[rate_class] = entry
        if per_class:
            changes["rate_changed"][model_id] = {"name": new.get("name"), "classes": per_class}

        old_blend = old.get("observed_blended_usd_per_mtok")
        new_blend = new.get("observed_blended_usd_per_mtok")
        if old_blend and new_blend:
            ratio = relative_change(old_blend, new_blend)
            if ratio is not None and abs(ratio) >= blend_threshold:
                changes["observed_blend_changed"][model_id] = {
                    "name": new.get("name"),
                    "from": old_blend,
                    "to": new_blend,
                    "change_ratio": round(ratio, 6),
                }

    return changes


def change_counts(changes):
    """Summary counts for the dashboard's weekly change panel."""
    return {
        "catalog_added": len(changes.get("catalog_added") or []),
        "catalog_removed": len(changes.get("catalog_removed") or []),
        "quality_changed": len(changes.get("quality_changed") or {}),
        "rate_changed": len(changes.get("rate_changed") or {}),
        "observed_blend_changed": len(changes.get("observed_blend_changed") or {}),
    }


def truncate_words(text, max_words=100):
    """Cap a summary at the documented word limit."""
    words = str(text or "").split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]) + " ..."


def deterministic_summary(changes, max_words=100):
    """A factual summary used when the narrative model is unavailable.

    It reports only what ``detect_changes`` actually found; nothing is inferred
    and no missing value is filled in.
    """
    if changes.get("first_run"):
        return "First recorded snapshot; there is no earlier snapshot to compare against."
    added = changes.get("catalog_added") or []
    removed = changes.get("catalog_removed") or []
    quality = changes.get("quality_changed") or {}
    rates = changes.get("rate_changed") or {}
    blends = changes.get("observed_blend_changed") or {}
    parts = []
    if added:
        parts.append(f"{len(added)} catalog addition(s): " + ", ".join(added))
    if removed:
        parts.append(f"{len(removed)} catalog removal(s): " + ", ".join(removed))
    if quality:
        details = [
            f"{entry.get('name') or model_id} {entry['from']} to {entry['to']}"
            for model_id, entry in sorted(quality.items())
        ]
        parts.append(f"{len(quality)} AA quality change(s): " + "; ".join(details))
    if rates:
        details = []
        for model_id, entry in sorted(rates.items()):
            classes = ", ".join(sorted(entry["classes"]))
            details.append(f"{entry.get('name') or model_id} ({classes})")
        parts.append(f"{len(rates)} rate change(s): " + "; ".join(details))
    if blends:
        details = [
            f"{entry.get('name') or model_id} {entry['change_ratio']:+.0%}"
            for model_id, entry in sorted(blends.items())
        ]
        parts.append(f"{len(blends)} observed billing-rate drift(s): " + "; ".join(details))
    if not parts:
        return "No catalog, quality, or billing-rate changes against the preceding snapshot."
    return truncate_words(". ".join(parts) + ".", max_words)
