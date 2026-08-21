import hashlib
import json
import re
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

REQUIRED_FILES = [
    "README.md",
    "training_manifest.json",
    "evaluation.json",
    "inventory.json",
    "adapter_model.safetensors",
    "adapter_config.json"
]

UNSAFE_EXTENSIONS = {".bin", ".pt", ".pth", ".pkl", ".pickle"}

def sha256_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def compact_json(obj) -> str:
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False)

@app.post("/verify-bundle")
async def verify_bundle(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})

    policy = body.get("policy")
    files = body.get("files")

    if not isinstance(body, dict) or not isinstance(policy, dict) or not isinstance(files, dict):
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})

    req_slices = policy.get("requiredSlices")
    license_val = policy.get("license")
    intended_use = policy.get("intendedUse")
    limitations = policy.get("limitations")

    if not isinstance(req_slices, list) or len(req_slices) == 0 or \
       not all(isinstance(s, str) and s.strip() and s == req_slices[i-1] for i, s in enumerate(req_slices) if i > 0) or \
       not isinstance(license_val, str) or not license_val.strip() or \
       not isinstance(intended_use, str) or not intended_use.strip() or \
       not isinstance(limitations, str) or not limitations.strip():
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})

    unique_slices = []
    seen_s = set()
    for s in req_slices:
        if s not in seen_s:
            seen_s.add(s)
            unique_slices.append(s)
    req_slices = unique_slices

    violations = set()

    for fname in REQUIRED_FILES:
        if fname not in files or not isinstance(files[fname], str):
            violations.add(f"MISSING_FILE:{fname}")

    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in files.items()):
        violations.add("INVALID_POLICY")

    unsafe_files = {k for k in files if any(k.endswith(ext) for ext in UNSAFE_EXTENSIONS)}
    for f in unsafe_files:
        violations.add("UNSAFE_WEIGHTS")

    inventory_digest = None
    inventory_valid = True

    if "inventory.json" not in violations:
        try:
            inv_data = json.loads(files["inventory.json"])
            if not isinstance(inv_data, list):
                violations.add("INVALID_JSON:inventory.json")
                inventory_valid = False
            else:
                expected_inv = []
                for fname in sorted(files.keys()):
                    if fname == "inventory.json":
                        continue
                    f_bytes = files[fname].encode('utf-8')
                    expected_inv.append({
                        "name": fname,
                        "bytes": len(f_bytes),
                        "sha256": sha256_hash(f_bytes)
                    })
                
                if compact_json(inv_data) != compact_json(expected_inv):
                    violations.add("INVENTORY_MISMATCH")
                
                tracked_files = {item.get("name") for item in inv_data if isinstance(item, dict)}
                for fname in files:
                    if fname != "inventory.json" and fname not in tracked_files:
                        violations.add("UNTRACKED_FILE")
                
                inventory_digest = sha256_hash(compact_json(expected_inv).encode('utf-8'))
        except Exception:
            violations.add("INVALID_JSON:inventory.json")
            inventory_valid = False

    manifest_data = None
    if "training_manifest.json" not in violations:
        try:
            manifest_data = json.loads(files["training_manifest.json"])
            if not isinstance(manifest_data, dict):
                violations.add("INVALID_TRAINING_MANIFEST")
                manifest_data = None
            else:
                req_manifest_fields = ["baseRevision", "task", "datasetDigest", "codeDigest", "trainingConfigDigest", "modelArtifactDigest", "evaluationArtifactDigest"]
                for mf in req_manifest_fields:
                    if not isinstance(manifest_data.get(mf), str) or not manifest_data[mf].strip():
                        violations.add(f"MISSING_MANIFEST_FIELD:{mf}")
                
                if "baseRevision" not in violations and (len(manifest_data.get("baseRevision", "")) != 40 or not re.fullmatch(r'[0-9a-f]{40}', manifest_data["baseRevision"])):
                    violations.add("MUTABLE_BASE_REVISION")
        except Exception:
            violations.add("INVALID_JSON:training_manifest.json")
            manifest_data = None

    adapter_config = None
    if "adapter_config.json" not in violations:
        try:
            adapter_config = json.loads(files["adapter_config.json"])
            if not isinstance(adapter_config, dict):
                violations.add("INVALID_ADAPTER_CONFIG")
                adapter_config = None
            else:
                r_val = adapter_config.get("r")
                if not isinstance(r_val, int) or r_val <= 0 or r_val > 2**53 - 1:
                    violations.add("INVALID_ADAPTER_CONFIG")
                
                tm = adapter_config.get("target_modules")
                if not isinstance(tm, list) or len(tm) == 0 or not all(isinstance(x, str) and x.strip() for x in tm):
                    violations.add("INVALID_ADAPTER_CONFIG")
                elif len(set(tm)) != len(tm):
                    violations.add("INVALID_ADAPTER_CONFIG")
        except Exception:
            violations.add("INVALID_JSON:adapter_config.json")
            adapter_config = None

    eval_data = None
    if "evaluation.json" not in violations:
        try:
            eval_data = json.loads(files["evaluation.json"])
            if not isinstance(eval_data, dict):
                violations.add("INVALID_EVALUATION")
                eval_data = None
            else:
                agg = eval_data.get("aggregate")
                if agg is None or not isinstance(agg, (int, float)) or not (0 <= agg <= 1) or agg != agg: 
                    violations.add("INVALID_AGGREGATE")
                
                for sl in req_slices:
                    sl_val = eval_data.get(sl)
                    if sl_val is None or not isinstance(sl_val, (int, float)) or not (0 <= sl_val <= 1) or sl_val != sl_val:
                        violations.add(f"MISSING_SLICE:{sl}")
                    elif not (0 <= sl_val <= 1):
                        violations.add(f"SLICE_RANGE:{sl}")
        except Exception:
            violations.add("INVALID_JSON:evaluation.json")
            eval_data = None

    if "adapter_model.safetensors" not in violations and manifest_data and "modelArtifactDigest" not in [v.split(":")[0] for v in violations]:
        actual_model_digest = sha256_hash(files["adapter_model.safetensors"].encode('utf-8'))
        if manifest_data.get("modelArtifactDigest") != actual_model_digest:
            violations.add("MODEL_ARTIFACT_MISMATCH")

    if "evaluation.json" not in violations and manifest_data and "evaluationArtifactDigest" not in [v.split(":")[0] for v in violations]:
        actual_eval_digest = sha256_hash(files["evaluation.json"].encode('utf-8'))
        if manifest_data.get("evaluationArtifactDigest") != actual_eval_digest:
            violations.add("EVALUATION_DIGEST_MISMATCH")

    if eval_data and manifest_data and "MODEL_ARTIFACT_MISMATCH" not in violations:
        eval_model_digest = eval_data.get("modelArtifactDigest")
        if eval_model_digest is not None and manifest_data.get("modelArtifactDigest") != eval_model_digest:
            violations.add("EVALUATION_ARTIFACT_MISMATCH")

    if "README.md" not in violations:
        readme = files["README.md"]
        matches = re.findall(r'<!--\s*tds-model-card\s+(.*?)\s*-->', readme, re.DOTALL)
        if len(matches) == 0:
            violations.add("MISSING_MODEL_CARD")
        elif len(matches) > 1:
            violations.add("MODEL_CARD_COUNT")
        else:
            payload = matches[0]
            try:
                card = json.loads(payload)
                if not isinstance(card, dict):
                    violations.add("INVALID_MODEL_CARD")
                else:
                    if manifest_data:
                        checks = {
                            "task": manifest_data.get("task"),
                            "baseRevision": manifest_data.get("baseRevision"),
                            "datasetDigest": manifest_data.get("datasetDigest"),
                            "modelArtifactDigest": manifest_data.get("modelArtifactDigest"),
                            "license": license_val,
                            "intendedUse": intended_use,
                            "limitations": limitations
                        }
                        for k, expected in checks.items():
                            if card.get(k) != expected:
                                violations.add("MODEL_CARD_MISMATCH")
                                break
            except json.JSONDecodeError:
                violations.add("INVALID_MODEL_CARD")

    sorted_violations = sorted(list(violations), key=lambda x: x.encode('utf-8'))

    return JSONResponse(content={
        "decision": "admit" if not sorted_violations else "reject",
        "violations": sorted_violations,
        "inventoryDigest": inventory_digest
    })