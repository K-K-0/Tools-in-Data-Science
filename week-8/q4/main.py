import math
import re
from typing import Any, Dict, List, Optional, Union

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

INTERVENTIONS = ["prompt_only", "retrieval", "lora", "qlora"]
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ---------------- helpers ----------------

def is_bool(x: Any) -> bool:
    return isinstance(x, bool)


def is_finite_num(x: Any) -> bool:
    # exclude bool (bool is subclass of int in python)
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


def is_safe_pos_int(x: Any) -> bool:
    return is_safe_int(x) and x > 0


def utf8_key(s: str):
    return s.encode("utf-8")


def sort_utf8(items: List[str]) -> List[str]:
    return sorted(items, key=utf8_key)


def sort_dedup_utf8(items: List[str]) -> List[str]:
    return sort_utf8(list(set(items)))


def round12(x: float) -> float:
    return round(x, 12)


# ---------------- CHOOSE ----------------

def validate_choose_input(body: Dict[str, Any]) -> bool:
    policy = body.get("policy")
    if not isinstance(policy, dict):
        return False

    required_policy_keys = [
        "minQuality", "freshnessRequired", "maxLatencyMs", "maxMemoryMb",
        "maxLabeledExamples", "maxTotalCost", "horizonRequests",
    ]
    for k in required_policy_keys:
        if k not in policy:
            return False

    min_quality = policy["minQuality"]
    if not is_finite_num(min_quality) or min_quality < 0 or min_quality > 1:
        return False
    if not is_bool(policy["freshnessRequired"]):
        return False
    if not is_finite_num(policy["maxLatencyMs"]) or policy["maxLatencyMs"] < 0:
        return False
    if not is_finite_num(policy["maxMemoryMb"]) or policy["maxMemoryMb"] < 0:
        return False
    if not is_safe_non_neg_int(policy["maxLabeledExamples"]):
        return False
    if not is_finite_num(policy["maxTotalCost"]) or policy["maxTotalCost"] < 0:
        return False
    if not is_safe_non_neg_int(policy["horizonRequests"]):
        return False

    candidates = body.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 4:
        return False

    seen_names = set()
    for c in candidates:
        if not isinstance(c, dict):
            return False
        name = c.get("name")
        if name not in INTERVENTIONS:
            return False
        if name in seen_names:
            return False
        seen_names.add(name)

        if not is_bool(c.get("available")):
            return False
        quality = c.get("quality")
        if not is_finite_num(quality) or quality < 0 or quality > 1:
            return False
        if not is_bool(c.get("freshness")):
            return False
        if not is_finite_num(c.get("latencyMs")) or c["latencyMs"] < 0:
            return False
        if not is_finite_num(c.get("memoryMb")) or c["memoryMb"] < 0:
            return False
        if not is_safe_non_neg_int(c.get("labeledExamples")):
            return False
        if not is_finite_num(c.get("oneTimeCost")) or c["oneTimeCost"] < 0:
            return False
        if not is_finite_num(c.get("recurringCost")) or c["recurringCost"] < 0:
            return False

    for name in INTERVENTIONS:
        if name not in seen_names:
            return False

    return True


def handle_choose(body: Dict[str, Any]) -> Dict[str, Any]:
    if not validate_choose_input(body):
        return {"status": 400, "body": {"error": "INVALID_INPUT"}}

    policy = body["policy"]
    cand_by_name = {c["name"]: c for c in body["candidates"]}

    total_costs: Dict[str, float] = {}
    reason_codes_by_name: Dict[str, List[str]] = {}
    eligible_set: List[str] = []

    for name in INTERVENTIONS:
        c = cand_by_name[name]
        total = round12(c["oneTimeCost"] + policy["horizonRequests"] * c["recurringCost"])
        total_costs[name] = total

        codes: List[str] = []
        if not c["available"]:
            codes.append("UNAVAILABLE")
        if c["quality"] < policy["minQuality"]:
            codes.append("QUALITY_FLOOR")
        if policy["freshnessRequired"] and not c["freshness"]:
            codes.append("FRESHNESS_REQUIRED")
        if c["latencyMs"] > policy["maxLatencyMs"]:
            codes.append("LATENCY_LIMIT")
        if c["memoryMb"] > policy["maxMemoryMb"]:
            codes.append("MEMORY_LIMIT")
        if c["labeledExamples"] > policy["maxLabeledExamples"]:
            codes.append("DATA_LIMIT")
        if total > policy["maxTotalCost"]:
            codes.append("COST_LIMIT")

        reason_codes_by_name[name] = sort_dedup_utf8(codes)
        if not codes:
            eligible_set.append(name)

    selected = eligible_set[0] if eligible_set else None

    return {
        "status": 200,
        "body": {
            "selected": selected,
            "eligible": eligible_set,
            "totalCosts": total_costs,
            "reasonCodes": reason_codes_by_name,
        },
    }


# ---------------- REPAIR ----------------

def handle_repair(body: Dict[str, Any]) -> Dict[str, Any]:
    reason_codes: set = set()

    # ---- Tokens / labels ----
    tokens = body.get("tokens")
    tokens_valid = isinstance(tokens, list) and len(tokens) > 0
    if tokens_valid:
        for t in tokens:
            if not isinstance(t, dict):
                tokens_valid = False
                break
            if not is_safe_non_neg_int(t.get("id")):
                tokens_valid = False
                break
            if t.get("role") not in ("system", "user", "assistant"):
                tokens_valid = False
                break
            if not is_bool(t.get("padding")):
                tokens_valid = False
                break
            if not isinstance(t.get("text"), str):
                tokens_valid = False
                break
    if not tokens_valid:
        reason_codes.add("INVALID_TOKEN")

    labels: List[int] = []
    if tokens_valid:
        labels = [
            t["id"] if (t["role"] == "assistant" and t["padding"] is False) else -100
            for t in tokens
        ]
    elif isinstance(tokens, list):
        labels = [-100 for _ in tokens]

    # ---- Template ----
    template_pass = body.get("templateApplications") == 1 and not isinstance(body.get("templateApplications"), bool)
    if not template_pass:
        reason_codes.add("CHAT_TEMPLATE_COUNT")

    # ---- Parameters / PEFT ----
    parameters = body.get("parameters")
    params_struct_valid = isinstance(parameters, list)
    allowed_targets = body.get("allowedTargets")
    allowed_valid = (
        isinstance(allowed_targets, list)
        and len(allowed_targets) > 0
        and all(isinstance(x, str) and len(x) > 0 for x in allowed_targets)
        and len(set(allowed_targets)) == len(allowed_targets)
    )

    trainable_params: List[str] = []
    trainable_count = 0
    peft_config_pass = True

    if not params_struct_valid or not allowed_valid:
        peft_config_pass = False
        reason_codes.add("INVALID_PARAMETER")
    else:
        names = set()
        all_valid = True
        for p in parameters:
            if not isinstance(p, dict):
                all_valid = False
                break
            name = p.get("name")
            if not isinstance(name, str) or len(name) == 0:
                all_valid = False
                break
            if name in names:
                all_valid = False
                break
            names.add(name)
            if not isinstance(p.get("target"), str):
                all_valid = False
                break
            if not is_safe_pos_int(p.get("numel")):
                all_valid = False
                break

        if not all_valid:
            peft_config_pass = False
            reason_codes.add("INVALID_PARAMETER")
        else:
            allowed_set = set(allowed_targets)
            lora_params = [
                p for p in parameters
                if p["target"] in allowed_set
                and (p["name"].endswith(".lora_A.weight") or p["name"].endswith(".lora_B.weight"))
            ]
            if not lora_params:
                peft_config_pass = False
                reason_codes.add("INVALID_PARAMETER")
            else:
                sorted_names = sort_utf8([p["name"] for p in lora_params])
                trainable_params = sorted_names
                numel_by_name = {p["name"]: p["numel"] for p in lora_params}
                total = 0
                for n in sorted_names:
                    total += numel_by_name[n]
                    if not is_safe_int(total):
                        total = 2**53 - 1
                        break
                trainable_count = total

    # ---- Inference mode / dropout / eval isolation ----
    eval_isolated = True
    evaluation_deterministic = True

    if body.get("inferenceMode") is not False:
        reason_codes.add("INFERENCE_MODE")
    if body.get("dropoutActiveDuringEval") is not False:
        reason_codes.add("EVAL_DROPOUT_ACTIVE")
        evaluation_deterministic = False

    train_row_ids = body.get("trainRowIds")
    eval_row_ids = body.get("evalRowIds")

    def ids_valid(arr):
        return (
            isinstance(arr, list)
            and len(arr) > 0
            and all(isinstance(x, str) and len(x) > 0 for x in arr)
            and len(set(arr)) == len(arr)
        )

    train_eval_ids_valid = ids_valid(train_row_ids) and ids_valid(eval_row_ids)
    if not train_eval_ids_valid:
        eval_isolated = False
        reason_codes.add("EVAL_LEAKAGE")
    else:
        train_set = set(train_row_ids)
        if any(i in train_set for i in eval_row_ids):
            eval_isolated = False
            reason_codes.add("EVAL_LEAKAGE")

    # ---- Artifact files ----
    artifact_files = body.get("artifactFiles")
    expected_set = ["adapter_config.json", "adapter_model.safetensors"]
    adapter_files: List[str] = []
    files_ok = isinstance(artifact_files, list) and len(artifact_files) == 2
    if files_ok:
        sorted_given = sort_utf8(artifact_files) if all(isinstance(f, str) for f in artifact_files) else None
        sorted_expected = sort_utf8(expected_set)
        if sorted_given == sorted_expected:
            adapter_files = sorted_given
        else:
            files_ok = False
    if not files_ok:
        reason_codes.add("ADAPTER_FILE_SET")
        if isinstance(artifact_files, list) and any(
            isinstance(f, str) and re.search(r"pytorch_model|model\.safetensors$|\.bin$", f) and f != "adapter_model.safetensors"
            for f in artifact_files
        ):
            reason_codes.add("FULL_MODEL_ARTIFACT")

    # ---- Lineage ----
    lineage_pass = True
    base_revision = body.get("baseRevision")
    if not isinstance(base_revision, str) or not HEX40.match(base_revision):
        lineage_pass = False
        reason_codes.add("MUTABLE_BASE_REVISION")

    dataset_digest = body.get("datasetDigest")
    code_digest = body.get("codeDigest")
    config_digest = body.get("configDigest")
    expected_digests = body.get("expectedDigests")

    digest_fields_valid = (
        isinstance(dataset_digest, str) and HEX64.match(dataset_digest)
        and isinstance(code_digest, str) and HEX64.match(code_digest)
        and isinstance(config_digest, str) and HEX64.match(config_digest)
    )

    if not digest_fields_valid:
        lineage_pass = False
        reason_codes.add("LINEAGE_MISMATCH")
    elif isinstance(expected_digests, dict):
        checks = [
            ("datasetDigest", dataset_digest),
            ("codeDigest", code_digest),
            ("configDigest", config_digest),
        ]
        for key, val in checks:
            if key in expected_digests and expected_digests[key] != val:
                lineage_pass = False
                reason_codes.add("LINEAGE_MISMATCH")

    # ---- Effective batch ----
    micro_batch = body.get("microBatch")
    gradient_accumulation = body.get("gradientAccumulation")
    replicas = body.get("replicas")
    expected_effective_batch = body.get("expectedEffectiveBatch")

    batch_fields_valid = (
        is_safe_pos_int(micro_batch)
        and is_safe_pos_int(gradient_accumulation)
        and is_safe_pos_int(replicas)
        and is_safe_pos_int(expected_effective_batch)
    )
    effective_batch_ok = False
    if batch_fields_valid:
        effective_batch_ok = (micro_batch * gradient_accumulation * replicas) == expected_effective_batch
    if not batch_fields_valid or not effective_batch_ok:
        reason_codes.add("EFFECTIVE_BATCH_MISMATCH")

    # ---- Checkpoint ----
    checkpoint = body.get("checkpoint")
    required_ck_keys = ["model", "optimizer", "scheduler", "step", "rng", "dataPosition"]
    checkpoint_complete = isinstance(checkpoint, dict) and all(k in checkpoint for k in required_ck_keys)
    if not checkpoint_complete:
        reason_codes.add("INCOMPLETE_CHECKPOINT")

    # ---- Resume ----
    uninterrupted_weights = body.get("uninterruptedWeights")
    resumed_weights = body.get("resumedWeights")
    resume_tolerance = body.get("resumeTolerance")

    def is_num_arr_valid(arr):
        return isinstance(arr, list) and len(arr) > 0 and all(is_finite_num(x) for x in arr)

    resume_pass = True
    arrays_valid = (
        is_num_arr_valid(uninterrupted_weights)
        and is_num_arr_valid(resumed_weights)
        and len(uninterrupted_weights) == len(resumed_weights)
    )
    tolerance_valid = is_finite_num(resume_tolerance) and resume_tolerance >= 0

    if not arrays_valid or not tolerance_valid:
        resume_pass = False
        reason_codes.add("RESUME_DIVERGENCE")
    else:
        for a, b in zip(uninterrupted_weights, resumed_weights):
            if abs(a - b) > resume_tolerance:
                resume_pass = False
                reason_codes.add("RESUME_DIVERGENCE")
                break

    return {
        "status": 200,
        "body": {
            "labels": labels,
            "templatePass": template_pass,
            "trainableParams": trainable_params,
            "trainableCount": trainable_count,
            "peftConfigPass": peft_config_pass,
            "adapterFiles": adapter_files,
            "checkpointComplete": checkpoint_complete,
            "lineagePass": lineage_pass,
            "evalIsolated": eval_isolated,
            "evaluationDeterministic": evaluation_deterministic,
            "resumePass": resume_pass,
            "reasonCodes": sort_dedup_utf8(list(reason_codes)),
        },
    }


# ---------------- ROUTES ----------------

@app.post("/adapt")
async def adapt(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})

    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})

    op = body.get("operation")
    try:
        if op == "choose":
            result = handle_choose(body)
        elif op == "repair":
            result = handle_repair(body)
        else:
            return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})
    except Exception:
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})

    return JSONResponse(status_code=result["status"], content=result["body"])


@app.get("/")
async def root():
    return {"status": "OK"}