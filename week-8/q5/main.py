import hashlib
import json
import math
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

# In-memory store: freezeId -> {"request": <original freeze body>, "response": <response body>}
FREEZE_STORE: Dict[str, Dict[str, Any]] = {}

VALID_FREEZE_CODES = {
    "INVALID_INPUT",
    "UNALLOWED_UNSUPPORTED_REASON",
    "NOT_LOADABLE",
    "CALIBRATION_MISMATCH",
    "TOKENIZER_MISMATCH",
}


# ---------------- generic helpers ----------------

def is_bool(x: Any) -> bool:
    return isinstance(x, bool)


def is_finite_num(x: Any) -> bool:
    if isinstance(x, bool):
        return False
    if not isinstance(x, (int, float)):
        return False
    return math.isfinite(x)


def is_safe_int(x: Any) -> bool:
    if isinstance(x, bool):
        return False
    if not isinstance(x, int):
        return False
    return -(2**53 - 1) <= x <= (2**53 - 1)


def is_safe_non_neg_int(x: Any) -> bool:
    return is_safe_int(x) and x >= 0


def utf8_key(s: str):
    return s.encode("utf-8")


def sort_utf8(items: List[str]) -> List[str]:
    return sorted(items, key=utf8_key)


def sort_dedup_utf8(items: List[str]) -> List[str]:
    return sort_utf8(list(set(items)))


def round12(x: float) -> float:
    return round(x, 12)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def non_empty_str(x: Any) -> bool:
    return isinstance(x, str) and len(x) > 0


def unique_non_empty_str_list(x: Any) -> bool:
    return (
        isinstance(x, list)
        and all(non_empty_str(i) for i in x)
        and len(set(x)) == len(x)
    )


# ---------------- FREEZE ----------------

def compute_inventory(files: Dict[str, str]) -> Tuple[List[Dict[str, Any]], int, str]:
    """files: dict of filename -> string content. Returns (inventory, totalBytes, packageDigest)."""
    names = sort_utf8(list(files.keys()))
    inventory = []
    total_bytes = 0
    for name in names:
        content = files[name]
        raw = content.encode("utf-8")
        b = len(raw)
        h = sha256_hex(raw)
        inventory.append({"name": name, "bytes": b, "sha256": h})
        total_bytes += b

    # compact JSON, exact key order name,bytes,sha256
    inventory_for_digest = [
        {"name": item["name"], "bytes": item["bytes"], "sha256": item["sha256"]}
        for item in inventory
    ]
    compact = json.dumps(inventory_for_digest, separators=(",", ":"), ensure_ascii=True)
    package_digest = sha256_hex(compact.encode("utf-8"))

    return inventory, total_bytes, package_digest


def validate_freeze_top_level(body: Dict[str, Any]) -> bool:
    freeze_id = body.get("freezeId")
    if not isinstance(freeze_id, str) or not (0 < len(freeze_id) <= 128):
        return False
    if not non_empty_str(body.get("calibrationDigest")):
        return False
    if not non_empty_str(body.get("tokenizerDigest")):
        return False

    allowed = body.get("allowedUnsupportedReasons")
    if not isinstance(allowed, list):
        return False
    if not all(non_empty_str(x) for x in allowed):
        return False
    if len(set(allowed)) != len(allowed):
        return False

    candidates = body.get("candidates")
    if not isinstance(candidates, list) or len(candidates) == 0:
        return False

    return True


def validate_candidate_files(files: Any) -> bool:
    if not isinstance(files, dict) or len(files) == 0:
        return False
    for k, v in files.items():
        if not isinstance(k, str) or len(k) == 0:
            return False
        if not isinstance(v, str):
            return False
    return True


def process_freeze_candidate(cand: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    name = cand.get("name")
    files = cand.get("files")

    files_valid = validate_candidate_files(files)

    if not files_valid:
        return {
            "name": name,
            "status": "invalid",
            "inventory": [],
            "totalBytes": None,
            "packageDigest": None,
            "reasonCodes": ["INVALID_INPUT"],
        }

    inventory, total_bytes, package_digest = compute_inventory(files)

    reason_codes = []
    allowed_reasons = set(body.get("allowedUnsupportedReasons", []))
    unsupported_reason = cand.get("unsupportedReason")

    has_reason = unsupported_reason is not None and isinstance(unsupported_reason, str) and len(unsupported_reason) > 0

    if has_reason:
        if unsupported_reason in allowed_reasons:
            status = "unsupported"
        else:
            status = "invalid"
            reason_codes.append("UNALLOWED_UNSUPPORTED_REASON")
    else:
        loadable = cand.get("loadable")
        cal_digest = cand.get("calibrationDigest")
        tok_digest = cand.get("tokenizerDigest")

        ok = True
        if loadable is not True:
            ok = False
            reason_codes.append("NOT_LOADABLE")
        if cal_digest != body.get("calibrationDigest"):
            ok = False
            reason_codes.append("CALIBRATION_MISMATCH")
        if tok_digest != body.get("tokenizerDigest"):
            ok = False
            reason_codes.append("TOKENIZER_MISMATCH")

        status = "frozen" if ok else "invalid"

    return {
        "name": name,
        "status": status,
        "inventory": inventory,
        "totalBytes": total_bytes,
        "packageDigest": package_digest,
        "reasonCodes": sort_dedup_utf8(reason_codes),
    }


def handle_freeze(body: Dict[str, Any]) -> Dict[str, Any]:
    if not validate_freeze_top_level(body):
        return {"status": 400, "body": {"error": "INVALID_INPUT"}}

    candidates = body["candidates"]
    freeze_id = body["freezeId"]

    # validate candidate-level structural requirements (name non-empty unique)
    names_seen = set()
    for c in candidates:
        if not isinstance(c, dict):
            return {"status": 400, "body": {"error": "INVALID_INPUT"}}
        name = c.get("name")
        if not non_empty_str(name):
            return {"status": 400, "body": {"error": "INVALID_INPUT"}}
        if name in names_seen:
            return {"status": 400, "body": {"error": "INVALID_INPUT"}}
        names_seen.add(name)

    processed = [process_freeze_candidate(c, body) for c in candidates]
    processed_sorted = sorted(processed, key=lambda x: utf8_key(x["name"]))

    response_body = {
        "freezeId": freeze_id,
        "candidates": processed_sorted,
    }

    existing = FREEZE_STORE.get(freeze_id)
    if existing is not None:
        if existing["request"] == body:
            return {"status": 200, "body": existing["response"]}
        else:
            return {"status": 409, "body": {"error": "FREEZE_ID_CONFLICT"}}

    FREEZE_STORE[freeze_id] = {"request": body, "response": response_body}
    return {"status": 200, "body": response_body}


# ---------------- SELECT ----------------

def validate_select_top_level(body: Dict[str, Any]) -> bool:
    candidates = body.get("candidates")
    rows = body.get("rows")
    policy = body.get("policy")

    if not isinstance(candidates, list):
        return False
    if not isinstance(rows, list):
        return False
    if not isinstance(policy, dict):
        return False
    return True


def validate_policy(policy: Dict[str, Any], candidate_names: set) -> Tuple[bool, Optional[str]]:
    required_keys = ["maxBytes", "aggregateFloor", "requiredSlices", "maxLatencyMs", "candidateOrder"]
    for k in required_keys:
        if k not in policy:
            return False, None

    if not is_safe_non_neg_int(policy["maxBytes"]):
        return False, None

    agg_floor = policy["aggregateFloor"]
    if not is_finite_num(agg_floor) or agg_floor < 0 or agg_floor > 1:
        return False, None

    required_slices = policy["requiredSlices"]
    if not isinstance(required_slices, dict):
        return False, None
    for slice_name, floor in required_slices.items():
        if not isinstance(slice_name, str) or len(slice_name) == 0:
            return False, None
        if not is_finite_num(floor) or floor < 0 or floor > 1:
            return False, None

    if not is_finite_num(policy["maxLatencyMs"]) or policy["maxLatencyMs"] < 0:
        return False, None

    candidate_order = policy["candidateOrder"]
    if not unique_non_empty_str_list(candidate_order):
        return False, None

    if set(candidate_order) != candidate_names:
        return False, None

    return True, None


def handle_select(body: Dict[str, Any]) -> Dict[str, Any]:
    if not validate_select_top_level(body):
        return {"status": 400, "body": {"error": "INVALID_INPUT"}}

    freeze_id = body.get("freezeId")
    if not non_empty_str(freeze_id):
        return {"status": 400, "body": {"error": "INVALID_INPUT"}}

    submitted_candidates = body["candidates"]
    rows = body["rows"]
    policy = body["policy"]
    latencies = body.get("latencies")

    stored = FREEZE_STORE.get(freeze_id)

    reason_codes_global = set()

    # names from submitted candidates (best-effort, for policy validation);
    # if submitted_candidates malformed, we still want to try policy validation later
    submitted_names = set()
    if all(isinstance(c, dict) and non_empty_str(c.get("name")) for c in submitted_candidates):
        submitted_names = {c["name"] for c in submitted_candidates}

    policy_ok, _ = validate_policy(policy, submitted_names)

    frozen_match = False
    stored_candidates_by_name: Dict[str, Any] = {}
    if stored is not None:
        stored_resp_candidates = stored["response"]["candidates"]
        if submitted_candidates == stored_resp_candidates:
            frozen_match = True
            stored_candidates_by_name = {c["name"]: c for c in stored_resp_candidates}

    if not frozen_match:
        reason_codes_global.add("NOT_FROZEN")

    if not policy_ok:
        reason_codes_global.add("INVALID_POLICY")

    if not isinstance(latencies, dict):
        latencies = {}

    # If names couldn't even be validated, use candidateOrder (if list) as fallback for ordering & results
    if submitted_names:
        working_names = submitted_names
    elif isinstance(policy.get("candidateOrder"), list):
        working_names = set(x for x in policy["candidateOrder"] if isinstance(x, str))
    else:
        working_names = set()

    candidate_order = policy.get("candidateOrder") if isinstance(policy.get("candidateOrder"), list) else []

    results = []
    admitted_infos = []  # (name, bytes, latency, order_index)

    for name in working_names:
        codes = set()
        codes |= reason_codes_global

        stored_cand = stored_candidates_by_name.get(name)

        # lineage / manifest validity
        lineage_valid = frozen_match and stored_cand is not None and stored_cand.get("status") == "frozen"
        if not lineage_valid:
            codes.add("INVALID_LINEAGE")

        # Recompute totalBytes / packageDigest from stored inventory (never trust submitted)
        total_bytes = None
        package_digest = None
        manifest_valid = False
        if stored_cand is not None and isinstance(stored_cand.get("inventory"), list):
            inv = stored_cand["inventory"]
            try:
                recomputed_total = sum(item["bytes"] for item in inv)
                inv_for_digest = [
                    {"name": item["name"], "bytes": item["bytes"], "sha256": item["sha256"]}
                    for item in inv
                ]
                compact = json.dumps(inv_for_digest, separators=(",", ":"), ensure_ascii=True)
                recomputed_digest = sha256_hex(compact.encode("utf-8"))
                total_bytes = recomputed_total
                package_digest = recomputed_digest
                manifest_valid = (
                    stored_cand.get("totalBytes") == recomputed_total
                    and stored_cand.get("packageDigest") == recomputed_digest
                )
            except Exception:
                manifest_valid = False

        if not manifest_valid:
            codes.add("INVALID_MANIFEST")
            total_bytes = None

        # predictions validity: every row must have binary (0/1) prediction for this candidate
        predictions_valid = True
        preds_by_row = []
        if len(rows) == 0:
            predictions_valid = False
        for row in rows:
            if not isinstance(row, dict):
                predictions_valid = False
                break
            label = row.get("label")
            slice_name = row.get("slice")
            preds = row.get("predictions")
            if label not in (0, 1) or isinstance(label, bool):
                predictions_valid = False
                break
            if not isinstance(slice_name, str) or len(slice_name) == 0:
                predictions_valid = False
                break
            if not isinstance(preds, dict) or name not in preds:
                predictions_valid = False
                break
            pred_val = preds[name]
            if pred_val not in (0, 1) or isinstance(pred_val, bool):
                predictions_valid = False
                break
            preds_by_row.append((label, slice_name, pred_val))

        if not predictions_valid:
            codes.add("INVALID_PREDICTIONS")

        aggregate = None
        slices_result = {}

        if predictions_valid:
            correct = sum(1 for (label, _, pred) in preds_by_row if label == pred)
            aggregate = round12(correct / len(preds_by_row))

            slice_totals: Dict[str, List[int]] = {}
            for (label, slice_name, pred) in preds_by_row:
                if slice_name not in slice_totals:
                    slice_totals[slice_name] = [0, 0]
                slice_totals[slice_name][1] += 1
                if label == pred:
                    slice_totals[slice_name][0] += 1

            required_slices = policy.get("requiredSlices", {}) if isinstance(policy.get("requiredSlices"), dict) else {}
            for slice_name, correct_count_total in slice_totals.items():
                correct_count, total_count = correct_count_total
                slices_result[slice_name] = round12(correct_count / total_count)

            # check required slices present & floors met
            for slice_name, floor in required_slices.items():
                if slice_name not in slice_totals:
                    codes.add(f"MISSING_SLICE:{slice_name}")
                else:
                    acc = slices_result[slice_name]
                    if acc < floor:
                        codes.add(f"SLICE_FLOOR:{slice_name}")

            if policy_ok:
                agg_floor = policy.get("aggregateFloor")
                if is_finite_num(agg_floor) and aggregate < agg_floor:
                    codes.add("AGGREGATE_FLOOR")
        else:
            aggregate = None
            slices_result = {}

        # size limit
        max_bytes = policy.get("maxBytes")
        size_ok = True
        if total_bytes is None:
            size_ok = False
        elif policy_ok and is_safe_non_neg_int(max_bytes):
            if total_bytes > max_bytes:
                size_ok = False
                codes.add("SIZE_LIMIT")

        # latency limit
        latency_val = latencies.get(name)
        latency_ok = True
        if not is_finite_num(latency_val) or latency_val < 0:
            latency_val = None
            latency_ok = False
        else:
            max_latency = policy.get("maxLatencyMs")
            if policy_ok and is_finite_num(max_latency):
                if latency_val > max_latency:
                    latency_ok = False
                    codes.add("LATENCY_LIMIT")

        if latency_val is None:
            latency_ok = False

        codes_sorted = sort_dedup_utf8(list(codes))
        admitted = len(codes_sorted) == 0

        try:
            order_index = candidate_order.index(name)
        except ValueError:
            order_index = len(candidate_order) + 1

        results.append({
            "name": name,
            "aggregate": aggregate,
            "slices": slices_result,
            "totalBytes": total_bytes,
            "latencyMs": latency_val,
            "admitted": admitted,
            "reasonCodes": codes_sorted,
            "_order_index": order_index,
        })

        if admitted:
            admitted_infos.append((name, total_bytes, latency_val, order_index))

    # order results by candidateOrder, fallback UTF-8 name
    def sort_key(r):
        return (r["_order_index"], utf8_key(r["name"]))

    results_sorted = sorted(results, key=sort_key)
    for r in results_sorted:
        del r["_order_index"]

    selected = None
    package_manifest = None
    if admitted_infos:
        admitted_infos.sort(key=lambda t: (t[1], t[2], t[3]))
        selected_name = admitted_infos[0][0]
        selected = selected_name
        winner_stored = stored_candidates_by_name.get(selected_name)
        package_manifest = winner_stored if winner_stored is not None else None

    return {
        "status": 200,
        "body": {
            "freezeId": freeze_id,
            "selected": selected,
            "results": results_sorted,
            "packageManifest": package_manifest,
        },
    }


# ---------------- ROUTES ----------------

@app.post("/quantize")
async def quantize(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})

    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})

    phase = body.get("phase")
    try:
        if phase == "freeze":
            result = handle_freeze(body)
        elif phase == "select":
            result = handle_select(body)
        else:
            return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})
    except Exception:
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})

    return JSONResponse(status_code=result["status"], content=result["body"])


@app.get("/")
async def root():
    return {"status": "OK"}