"""
/promote service (Python / FastAPI)

Run locally:
    pip install fastapi uvicorn
    uvicorn promote_service:app --host 0.0.0.0 --port 8000

POST /promote

This endpoint is implemented as a pure, deterministic function of its
input: given byte-identical input it always returns byte-identical
output (same action, same aliasMutation), which is what satisfies the
"replaying after that alias change must retain it" idempotency
requirement -- there is no external mutable state (no runId here, so
nothing to persist against).
"""

import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

# ---------------------------------------------------------------------------
# UTF-8 byte helpers
# ---------------------------------------------------------------------------


def utf8_bytes(s: str) -> bytes:
    return s.encode("utf-8")


# ---------------------------------------------------------------------------
# Instant validation / parsing -> epoch millis
# ---------------------------------------------------------------------------

_DT_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,3})?(Z|[+-]\d{2}:\d{2})$"
)


def _is_leap(y: int) -> bool:
    return (y % 4 == 0 and y % 100 != 0) or (y % 400 == 0)


_DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def parse_instant(s: Any):
    if not isinstance(s, str):
        return None
    m = _DT_RE.match(s)
    if not m:
        return None
    year, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
    frac = m.group(7)
    offset = m.group(8)

    if not (1 <= month <= 12):
        return None
    max_day = _DAYS_IN_MONTH[month - 1]
    if month == 2 and _is_leap(year):
        max_day = 29
    if not (1 <= day <= max_day):
        return None
    if hour > 23 or minute > 59 or second > 59:
        return None

    offset_minutes = 0
    if offset != "Z":
        sign = -1 if offset[0] == "-" else 1
        oh = int(offset[1:3])
        om = int(offset[4:6])
        if oh > 14 or om > 59:
            return None
        if oh == 14 and om != 0:
            return None
        offset_minutes = sign * (oh * 60 + om)

    ms = 0
    if frac:
        frac_digits = frac[1:]
        padded = (frac_digits + "000")[:3]
        ms = int(padded)

    try:
        dt = datetime(year, month, day, hour, minute, second, ms * 1000, tzinfo=timezone.utc)
    except ValueError:
        return None

    return int(dt.timestamp() * 1000) - offset_minutes * 60000


# ---------------------------------------------------------------------------
# Generic numeric validators
# ---------------------------------------------------------------------------


def is_number(n: Any) -> bool:
    return isinstance(n, (int, float)) and not isinstance(n, bool)


def is_finite_number(n: Any) -> bool:
    if not is_number(n):
        return False
    f = float(n)
    return f == f and f not in (float("inf"), float("-inf"))


def is_finite_unit_interval(n: Any) -> bool:
    return is_finite_number(n) and 0 <= float(n) <= 1


def is_nonneg_safe_int(n: Any) -> bool:
    if isinstance(n, bool):
        return False
    if not isinstance(n, int):
        return False
    return 0 <= n <= 2**53 - 1


def is_nonempty_str(s: Any) -> bool:
    return isinstance(s, str) and len(s) > 0


def round12(x: float) -> float:
    return round(x + 0.0, 12)


# ---------------------------------------------------------------------------
# Version canonicalization
# ---------------------------------------------------------------------------

_CANON_VERSION_RE = re.compile(r"^[1-9][0-9]*$")


def is_canonical_version(v: Any) -> bool:
    return isinstance(v, str) and bool(_CANON_VERSION_RE.match(v))


def version_group_key(v: Any):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    try:
        return json.dumps(v, sort_keys=True, ensure_ascii=False)
    except TypeError:
        return repr(v)


def failed_gates_key(v: Any) -> str:
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except TypeError:
        return str(v)


# ---------------------------------------------------------------------------
# Policy validation (field-by-field, independent)
# ---------------------------------------------------------------------------


def validate_policy_fields(policy: dict):
    """Returns dict of field -> bool valid, plus overall bool."""
    result = {}
    result["datasetDigest"] = is_nonempty_str(policy.get("datasetDigest"))
    result["schemaDigest"] = is_nonempty_str(policy.get("schemaDigest"))
    result["maxAgeSeconds"] = is_nonneg_safe_int(policy.get("maxAgeSeconds"))
    result["accuracyFloor"] = is_finite_unit_interval(policy.get("accuracyFloor"))
    result["maxLatencyMs"] = is_finite_number(policy.get("maxLatencyMs")) and float(
        policy.get("maxLatencyMs", -1)
    ) >= 0
    result["maxSizeBytes"] = is_nonneg_safe_int(policy.get("maxSizeBytes"))
    result["minImprovement"] = is_finite_unit_interval(policy.get("minImprovement"))

    required_slices = policy.get("requiredSlices")
    slices_valid = isinstance(required_slices, dict) and all(
        isinstance(k, str) and is_finite_unit_interval(v) for k, v in required_slices.items()
    )
    result["requiredSlices"] = slices_valid

    overall = all(result.values())
    return result, overall


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------


def process_promote(body: dict):
    as_of = body.get("asOf")
    champion_version_in = body.get("championVersion")
    policy = body.get("policy")
    versions = body.get("versions")

    as_of_millis = parse_instant(as_of)

    field_valid, policy_overall_valid = validate_policy_fields(policy)

    dataset_digest = policy.get("datasetDigest")
    schema_digest = policy.get("schemaDigest")
    max_age_seconds = policy.get("maxAgeSeconds")
    accuracy_floor = policy.get("accuracyFloor")
    max_latency_ms = policy.get("maxLatencyMs")
    max_size_bytes = policy.get("maxSizeBytes")
    min_improvement = policy.get("minImprovement")
    required_slices = policy.get("requiredSlices") if field_valid["requiredSlices"] else {}

    # -------- Pass 1: canonical / duplicate rejection --------
    raw_keys = [version_group_key(v.get("version") if isinstance(v, dict) else None) for v in versions]
    counts = Counter(raw_keys)

    entries = []  # list of dicts: {raw, canonical, codes:set(), evaluation, artifactDigest}
    excluded_keys = set()  # version strings excluded from lookup maps

    for v, key in zip(versions, raw_keys):
        codes = set()
        v_field = v.get("version") if isinstance(v, dict) else None
        canonical = is_canonical_version(v_field)
        is_dup = counts[key] > 1

        if not canonical:
            codes.add("INVALID_VERSION")
        if is_dup:
            codes.add("DUPLICATE_VERSION")

        gkey = failed_gates_key(v_field)

        if codes:
            entries.append({"key": gkey, "excluded": True, "codes": codes, "raw": v})
            excluded_keys.add(gkey)
            continue

        entries.append({"key": gkey, "excluded": False, "codes": set(), "raw": v})

    # -------- Pass 2: full evaluation for non-excluded entries --------
    lookup = {}  # version string -> computed info (accuracy, latency, size, evaluation)

    for entry in entries:
        if entry["excluded"]:
            continue
        v = entry["raw"]
        codes = entry["codes"]
        version_str = v.get("version")
        artifact_digest = v.get("artifactDigest")
        evaluation = v.get("evaluation")

        if not isinstance(evaluation, dict):
            codes.add("MISSING_EVALUATION")
            continue

        created_at = evaluation.get("createdAt")
        created_millis = parse_instant(created_at)
        if created_millis is None:
            codes.add("INVALID_TIMESTAMP")

        accuracy = evaluation.get("accuracy")
        latency_ms = evaluation.get("latencyMs")
        size_bytes = evaluation.get("sizeBytes")

        finite_ok = is_finite_number(accuracy) and is_finite_number(latency_ms) and is_finite_number(size_bytes)
        if not finite_ok:
            codes.add("NON_FINITE")

        range_ok = True
        if finite_ok:
            if not (0 <= float(accuracy) <= 1):
                range_ok = False
            if float(latency_ms) < 0:
                range_ok = False
            if not is_nonneg_safe_int(size_bytes):
                range_ok = False
            if not range_ok:
                codes.add("METRIC_RANGE")

        # digest bindings
        eval_artifact_digest = evaluation.get("artifactDigest")
        if eval_artifact_digest != artifact_digest or not is_nonempty_str(eval_artifact_digest):
            codes.add("ARTIFACT_MISMATCH")

        if field_valid["datasetDigest"]:
            eval_dataset_digest = evaluation.get("datasetDigest")
            if eval_dataset_digest != dataset_digest:
                codes.add("DATASET_MISMATCH")

        if field_valid["schemaDigest"]:
            eval_schema_digest = evaluation.get("schemaDigest")
            if eval_schema_digest != schema_digest:
                codes.add("SCHEMA_MISMATCH")

        # window check
        if created_millis is not None and as_of_millis is not None and field_valid["maxAgeSeconds"]:
            if created_millis > as_of_millis:
                codes.add("FUTURE_EVALUATION")
            elif created_millis < as_of_millis - max_age_seconds * 1000:
                codes.add("STALE_EVALUATION")

        # required slices
        eval_slices = evaluation.get("slices")
        if not isinstance(eval_slices, dict):
            eval_slices = {}
        if field_valid["requiredSlices"]:
            for sname, floor in required_slices.items():
                if sname not in eval_slices:
                    codes.add(f"MISSING_SLICE:{sname}")
                    continue
                sval = eval_slices[sname]
                if not is_finite_unit_interval(sval):
                    codes.add(f"SLICE_RANGE:{sname}")
                    continue
                if float(sval) < float(floor):
                    codes.add(f"SLICE_FLOOR:{sname}")

        # aggregate gates
        if finite_ok and range_ok:
            if field_valid["accuracyFloor"] and float(accuracy) < float(accuracy_floor):
                codes.add("ACCURACY_FLOOR")
            if field_valid["maxLatencyMs"] and float(latency_ms) > float(max_latency_ms):
                codes.add("LATENCY_LIMIT")
            if field_valid["maxSizeBytes"] and size_bytes > max_size_bytes:
                codes.add("SIZE_LIMIT")

        if not policy_overall_valid:
            codes.add("INVALID_POLICY")

        if not codes and finite_ok and range_ok:
            lookup[version_str] = {
                "accuracy": float(accuracy),
                "latency": float(latency_ms),
                "size": int(size_bytes),
                "evaluation": evaluation,
            }

    # entries excluded at pass 1 also get INVALID_POLICY if policy invalid (independent code)
    for entry in entries:
        if entry["excluded"] and not policy_overall_valid:
            entry["codes"].add("INVALID_POLICY")

    # -------- Assemble failedGates / eligibleVersions --------
    failed_gates = {}
    for entry in entries:
        if entry["codes"]:
            failed_gates[entry["key"]] = sorted(entry["codes"], key=utf8_bytes)

    eligible_versions = sorted(lookup.keys(), key=lambda s: int(s))

    # -------- Champion eligibility --------
    champion_eligible = (
        isinstance(champion_version_in, str)
        and champion_version_in in lookup
    )

    if not champion_eligible:
        return {
            "action": "block",
            "championVersion": champion_version_in,
            "selectedVersion": None,
            "eligibleVersions": eligible_versions,
            "failedGates": failed_gates,
            "aliasMutation": None,
            "evidence": None,
        }

    champion_info = lookup[champion_version_in]

    # -------- Ranking --------
    def rank_key(vstr):
        info = lookup[vstr]
        return (-info["accuracy"], info["latency"], info["size"], int(vstr))

    ranked = sorted(lookup.keys(), key=rank_key)
    top_candidate = ranked[0]

    if top_candidate == champion_version_in:
        return {
            "action": "retain",
            "championVersion": champion_version_in,
            "selectedVersion": champion_version_in,
            "eligibleVersions": eligible_versions,
            "failedGates": failed_gates,
            "aliasMutation": None,
            "evidence": champion_info["evaluation"],
        }

    challenger_info = lookup[top_candidate]
    delta = round12(challenger_info["accuracy"] - champion_info["accuracy"])

    if delta >= min_improvement if field_valid["minImprovement"] else False:
        return {
            "action": "promote",
            "championVersion": champion_version_in,
            "selectedVersion": top_candidate,
            "eligibleVersions": eligible_versions,
            "failedGates": failed_gates,
            "aliasMutation": {"alias": "champion", "version": top_candidate},
            "evidence": challenger_info["evaluation"],
        }
    else:
        return {
            "action": "retain",
            "championVersion": champion_version_in,
            "selectedVersion": champion_version_in,
            "eligibleVersions": eligible_versions,
            "failedGates": failed_gates,
            "aliasMutation": None,
            "evidence": champion_info["evaluation"],
        }


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@app.post("/promote")
async def promote(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})

    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})

    policy = body.get("policy")
    versions = body.get("versions")
    champion_version = body.get("championVersion")

    if not isinstance(policy, dict):
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})
    if not isinstance(versions, list):
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})
    if not isinstance(champion_version, str):
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})

    # Every entry in versions must at least be a dict to proceed meaningfully;
    # non-dict entries are treated as invalid/uncanonical version records.
    normalized_versions = []
    for v in versions:
        if isinstance(v, dict):
            normalized_versions.append(v)
        else:
            normalized_versions.append({"version": None})
    body = dict(body)
    body["versions"] = normalized_versions

    response = process_promote(body)
    return JSONResponse(status_code=200, content=response)