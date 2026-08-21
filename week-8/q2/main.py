"""
/bqml service (Python / FastAPI)

Run locally:
    pip install fastapi uvicorn
    uvicorn bqml_service:app --host 0.0.0.0 --port 8000

POST /bqml   (phase: "select" | "evaluate")

NOTE on persistence: this uses a simple in-process dict to remember
"select" phase responses keyed by runId, so that:
  - an identical replay of a select request returns the same response
  - a different select request reusing the same runId returns 409
  - a subsequent "evaluate" call can validate lineage against the stored
    successful selection.
This is process-local (not durable across restarts / multiple instances).
If you deploy behind multiple workers/replicas you'll need to swap this
for a shared store (e.g. Redis, a KV, or a database) keyed the same way.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

# ---------------------------------------------------------------------------
# In-memory persistence for select-phase responses, keyed by runId.
# ---------------------------------------------------------------------------

_SELECT_STORE: dict[str, dict] = {}
# runId -> {"input": <parsed request body>, "response": <response dict>}


# ---------------------------------------------------------------------------
# UTF-8 byte helpers
# ---------------------------------------------------------------------------


def utf8_bytes(s: str) -> bytes:
    return s.encode("utf-8")


def compare_utf8_bytes(a: str, b: str) -> int:
    ba, bb = utf8_bytes(a), utf8_bytes(b)
    if ba < bb:
        return -1
    if ba > bb:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Datetime validation / normalization  (same rules as the corpus task)
# ---------------------------------------------------------------------------

_DT_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,3})?(Z|[+-]\d{2}:\d{2})$"
)


def _is_leap(y: int) -> bool:
    return (y % 4 == 0 and y % 100 != 0) or (y % 400 == 0)


_DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def parse_instant(s: Any):
    """Returns epoch millis (int) or None if invalid."""
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

    epoch_millis = int(dt.timestamp() * 1000) - offset_minutes * 60000
    return epoch_millis


# ---------------------------------------------------------------------------
# Shared validators
# ---------------------------------------------------------------------------


def is_safe_int(n: Any) -> bool:
    if isinstance(n, bool):
        return False
    if not isinstance(n, int):
        return False
    return abs(n) <= 2**53 - 1


def is_nonneg_safe_int(n: Any) -> bool:
    return is_safe_int(n) and n >= 0


def is_finite_number(n: Any) -> bool:
    if isinstance(n, bool):
        return False
    if not isinstance(n, (int, float)):
        return False
    f = float(n)
    return f == f and f not in (float("inf"), float("-inf"))


def is_nonempty_str_le(s: Any, max_len: int) -> bool:
    return isinstance(s, str) and 0 < len(s) <= max_len


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def is_hex64(s: Any) -> bool:
    return isinstance(s, str) and bool(_HEX64_RE.match(s))


def compact_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# SELECT PHASE
# ---------------------------------------------------------------------------

_SPLIT_VALUES = {"TRAIN", "EVAL"}
_STATUS_VALUES = {"SUCCEEDED", "FAILED"}

_SELECT_ROW_KEYS = {"id", "entity", "eventTime", "predictionTime", "version", "split", "features"}
_TRIAL_KEYS = {"trialId", "status", "evalMetric"}


def validate_select_row_shape(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    if not _SELECT_ROW_KEYS.issubset(row.keys()):
        return False
    if not isinstance(row["id"], str) or row["id"] == "":
        return False
    if not isinstance(row["entity"], str):
        return False
    if parse_instant(row["eventTime"]) is None:
        return False
    if parse_instant(row["predictionTime"]) is None:
        return False
    if not is_nonneg_safe_int(row["version"]):
        return False
    if row["split"] not in _SPLIT_VALUES:
        return False
    features = row["features"]
    if not isinstance(features, dict):
        return False
    for fname, fval in features.items():
        if not isinstance(fname, str):
            return False
        if not isinstance(fval, dict):
            return False
        if not {"value", "availableAt"}.issubset(fval.keys()):
            return False
        if "value" not in fval:
            return False
        if parse_instant(fval["availableAt"]) is None:
            return False
    return True


def validate_trial_shape(trial: Any) -> bool:
    if not isinstance(trial, dict):
        return False
    if not _TRIAL_KEYS.issubset(trial.keys()):
        return False
    if not is_nonneg_safe_int(trial["trialId"]):
        return False
    if trial["status"] not in _STATUS_VALUES:
        return False
    if isinstance(trial["evalMetric"], bool) or not isinstance(trial["evalMetric"], (int, float)):
        return False
    return True


def validate_select_input(body: dict) -> bool:
    """Structural / shape validation only -> True if well-formed."""
    if not isinstance(body, dict):
        return False

    run_id = body.get("runId")
    if not is_nonempty_str_le(run_id, 128):
        return False

    forbidden = body.get("forbiddenFeatures")
    if not isinstance(forbidden, list) or any(not isinstance(x, str) for x in forbidden):
        return False

    limit = body.get("numTrialsLimit")
    if not is_safe_int(limit) or limit <= 0:
        return False

    rows = body.get("rows")
    if not isinstance(rows, list) or len(rows) == 0:
        return False
    ids_seen = set()
    for r in rows:
        if not validate_select_row_shape(r):
            return False
        if r["id"] in ids_seen:
            return False
        ids_seen.add(r["id"])

    trials = body.get("trials")
    if not isinstance(trials, list):
        return False
    trial_ids_seen = set()
    for t in trials:
        if not validate_trial_shape(t):
            return False
        if t["trialId"] in trial_ids_seen:
            return False
        trial_ids_seen.add(t["trialId"])

    return True


def process_select(body: dict):
    run_id = body.get("runId") if isinstance(body, dict) else None
    run_id_out = run_id if isinstance(run_id, str) else None

    if not validate_select_input(body):
        response = {
            "runId": run_id_out,
            "selectedTrialId": None,
            "trainRowIds": [],
            "evalRowIds": [],
            "featureNames": [],
            "datasetDigest": None,
            "reasonCodes": ["INVALID_INPUT"],
        }
        return 200, response, run_id_out

    forbidden = set(body["forbiddenFeatures"])
    num_trials_limit = body["numTrialsLimit"]
    rows = body["rows"]
    trials = body["trials"]

    # ---------------- Dedup rows by [entity, UTC(eventTime)] ----------------
    groups: dict[tuple, list] = {}
    for r in rows:
        event_millis = parse_instant(r["eventTime"])
        key = (r["entity"], event_millis)
        groups.setdefault(key, []).append(r)

    retained = []
    for grp in groups.values():
        if len(grp) == 1:
            retained.append(grp[0])
            continue
        winner = grp[0]
        for cand in grp[1:]:
            if cand["version"] > winner["version"]:
                winner = cand
            elif cand["version"] == winner["version"]:
                if compare_utf8_bytes(cand["id"], winner["id"]) < 0:
                    winner = cand
        retained.append(winner)

    # ---------------- Feature eligibility ----------------
    if retained:
        common_names = set(retained[0]["features"].keys())
        for r in retained[1:]:
            common_names &= set(r["features"].keys())
    else:
        common_names = set()

    eligible_features = []
    for name in common_names:
        if name in forbidden:
            continue
        ok = True
        for r in retained:
            avail_millis = parse_instant(r["features"][name]["availableAt"])
            pred_millis = parse_instant(r["predictionTime"])
            if avail_millis is None or pred_millis is None or avail_millis > pred_millis:
                ok = False
                break
        if ok:
            eligible_features.append(name)

    feature_names = sorted(eligible_features, key=utf8_bytes)

    train_row_ids = sorted(
        [r["id"] for r in retained if r["split"] == "TRAIN"], key=utf8_bytes
    )
    eval_row_ids = sorted(
        [r["id"] for r in retained if r["split"] == "EVAL"], key=utf8_bytes
    )

    # ---------------- Trials ----------------
    reason_codes = set()

    if len(trials) > num_trials_limit:
        reason_codes.add("TRIAL_LIMIT_EXCEEDED")

    eligible_trials = [
        t for t in trials if t["status"] == "SUCCEEDED" and is_finite_number(t["evalMetric"])
    ]

    selected_trial_id = None
    if not eligible_trials:
        reason_codes.add("NO_SUCCESSFUL_TRIAL")
    else:
        best = eligible_trials[0]
        for t in eligible_trials[1:]:
            if t["evalMetric"] > best["evalMetric"]:
                best = t
            elif t["evalMetric"] == best["evalMetric"]:
                if t["trialId"] < best["trialId"]:
                    best = t
        selected_trial_id = best["trialId"]

    # ---------------- datasetDigest ----------------
    digest_payload = {
        "trainRowIds": train_row_ids,
        "evalRowIds": eval_row_ids,
        "featureNames": feature_names,
    }
    dataset_digest = hashlib.sha256(
        utf8_bytes(compact_json(digest_payload))
    ).hexdigest()

    final_selected_trial_id = selected_trial_id if not reason_codes else None

    response = {
        "runId": run_id_out,
        "selectedTrialId": final_selected_trial_id,
        "trainRowIds": train_row_ids,
        "evalRowIds": eval_row_ids,
        "featureNames": feature_names,
        "datasetDigest": dataset_digest,
        "reasonCodes": sorted(reason_codes, key=utf8_bytes),
    }
    return 200, response, run_id_out


# ---------------------------------------------------------------------------
# EVALUATE PHASE
# ---------------------------------------------------------------------------


def validate_eval_row_shape(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    if not {"label", "prediction", "slice"}.issubset(row.keys()):
        return False
    label = row["label"]
    pred = row["prediction"]
    if isinstance(label, bool) or label not in (0, 1):
        return False
    if isinstance(pred, bool) or pred not in (0, 1):
        return False
    if not isinstance(row["slice"], str) or row["slice"] == "":
        return False
    return True


def round12(x: float) -> float:
    return round(x + 0.0, 12)


def process_evaluate(body: dict):
    codes = set()

    run_id = body.get("runId") if isinstance(body, dict) else None
    selected_trial_id = body.get("selectedTrialId") if isinstance(body, dict) else None
    dataset_digest = body.get("datasetDigest") if isinstance(body, dict) else None
    bytes_processed_raw = body.get("bytesProcessed") if isinstance(body, dict) else None
    max_bytes_raw = body.get("maxBytes") if isinstance(body, dict) else None

    core_valid = True
    if not isinstance(body, dict):
        core_valid = False
    if not is_nonempty_str_le(run_id, 128):
        core_valid = False
    if not is_nonneg_safe_int(selected_trial_id):
        core_valid = False
    if not is_hex64(dataset_digest):
        core_valid = False

    metric_floor = body.get("metricFloor") if isinstance(body, dict) else None
    if not is_finite_number(metric_floor) or not (0 <= float(metric_floor) <= 1):
        core_valid = False

    required_slices = body.get("requiredSlices") if isinstance(body, dict) else None
    if not isinstance(required_slices, dict):
        core_valid = False
    else:
        for k, v in required_slices.items():
            if not isinstance(k, str):
                core_valid = False
                break
            if not is_finite_number(v) or not (0 <= float(v) <= 1):
                core_valid = False
                break

    rows = body.get("rows") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        core_valid = False

    bytes_processed_valid = is_nonneg_safe_int(bytes_processed_raw)
    max_bytes_valid = is_nonneg_safe_int(max_bytes_raw)
    if not bytes_processed_valid or not max_bytes_valid:
        core_valid = False

    if not core_valid:
        codes.add("INVALID_INPUT")

    # ---------------- Row content validation ----------------
    rows_ok = isinstance(rows, list)
    any_row_invalid = False
    if rows_ok:
        for r in rows:
            if not validate_eval_row_shape(r):
                any_row_invalid = True
                break
    else:
        any_row_invalid = True

    if any_row_invalid and rows_ok and len(rows) > 0:
        codes.add("INVALID_TEST_ROW")
    elif not rows_ok:
        # rows itself malformed -> already covered by INVALID_INPUT
        pass

    valid_rows_present = rows_ok and len(rows) > 0 and not any_row_invalid

    # ---------------- Lineage check ----------------
    lineage_ok = False
    if is_nonempty_str_le(run_id, 128) and is_nonneg_safe_int(selected_trial_id) and is_hex64(dataset_digest):
        stored = _SELECT_STORE.get(run_id)
        if stored is not None:
            stored_resp = stored["response"]
            if (
                stored_resp.get("reasonCodes") == []
                and stored_resp.get("selectedTrialId") == selected_trial_id
                and stored_resp.get("datasetDigest") == dataset_digest
            ):
                lineage_ok = True
    if not lineage_ok:
        codes.add("INVALID_LINEAGE")

    # ---------------- Byte check ----------------
    if bytes_processed_valid and max_bytes_valid:
        if bytes_processed_raw > max_bytes_raw:
            codes.add("BYTE_LIMIT")

    # ---------------- Aggregate / slice checks ----------------
    test_metric = None
    if valid_rows_present:
        n = len(rows)
        correct = sum(1 for r in rows if r["label"] == r["prediction"])
        aggregate = round12(correct / n)
        test_metric = aggregate

        if core_valid:
            if aggregate < float(metric_floor):
                codes.add("AGGREGATE_FLOOR")

            for slice_name, floor in required_slices.items():
                slice_rows = [r for r in rows if r["slice"] == slice_name]
                if not slice_rows:
                    codes.add(f"MISSING_SLICE:{slice_name}")
                    continue
                s_correct = sum(1 for r in slice_rows if r["label"] == r["prediction"])
                s_acc = round12(s_correct / len(slice_rows))
                if s_acc < float(floor):
                    codes.add(f"SLICE_FLOOR:{slice_name}")
    else:
        test_metric = None

    decision = "admit" if len(codes) == 0 else "reject"

    critical_slice_pass = not (
        "INVALID_INPUT" in codes
        or "INVALID_LINEAGE" in codes
        or "INVALID_TEST_ROW" in codes
        or any(c.startswith("MISSING_SLICE:") for c in codes)
        or any(c.startswith("SLICE_FLOOR:") for c in codes)
    )

    bytes_processed_out = bytes_processed_raw if bytes_processed_valid else bytes_processed_raw

    response = {
        "runId": run_id if isinstance(run_id, str) else None,
        "selectedTrialId": selected_trial_id if is_nonneg_safe_int(selected_trial_id) else selected_trial_id,
        "datasetDigest": dataset_digest,
        "testMetric": test_metric,
        "criticalSlicePass": critical_slice_pass,
        "decision": decision,
        "bytesProcessed": bytes_processed_out,
        "reasonCodes": sorted(codes, key=utf8_bytes),
    }
    return 200, response


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@app.post("/bqml")
async def bqml(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})

    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})

    phase = body.get("phase")

    if phase == "select":
        status, response, run_id_out = process_select(body)

        if run_id_out is not None:
            existing = _SELECT_STORE.get(run_id_out)
            if existing is not None:
                if existing["input"] == body:
                    return JSONResponse(status_code=200, content=existing["response"])
                else:
                    return JSONResponse(
                        status_code=409, content={"error": "RUN_ID_CONFLICT"}
                    )
            _SELECT_STORE[run_id_out] = {"input": body, "response": response}

        return JSONResponse(status_code=status, content=response)

    elif phase == "evaluate":
        status, response = process_evaluate(body)
        return JSONResponse(status_code=status, content=response)

    else:
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})