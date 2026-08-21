"""
build-corpus service (Python / FastAPI)

Run locally:
    pip install fastapi uvicorn
    uvicorn main:app --host 0.0.0.0 --port 8000

POST /build-corpus
"""

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

# ---------------------------------------------------------------------------
# CRC32C (Castagnoli)
# ---------------------------------------------------------------------------

_CRC32C_POLY = 0x82F63B78  # reversed polynomial


def _build_crc32c_table():
    table = []
    for n in range(256):
        c = n
        for _ in range(8):
            if c & 1:
                c = _CRC32C_POLY ^ (c >> 1)
            else:
                c = c >> 1
        table.append(c & 0xFFFFFFFF)
    return table


_CRC32C_TABLE = _build_crc32c_table()


def crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc = _CRC32C_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF


def crc32c_hex(data: bytes) -> str:
    return format(crc32c(data), "08x")


# ---------------------------------------------------------------------------
# UTF-8 byte comparison helpers
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


def utf8_sort_key(s: str) -> bytes:
    return utf8_bytes(s)


# ---------------------------------------------------------------------------
# Datetime validation / normalization
# ---------------------------------------------------------------------------

_DT_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,3})?(Z|[+-]\d{2}:\d{2})$"
)


def _is_leap(y: int) -> bool:
    return (y % 4 == 0 and y % 100 != 0) or (y % 400 == 0)


_DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def parse_datetime(s: Any):
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


def normalize_datetime(s: Any):
    """Returns (iso_string, epoch_millis) or None."""
    epoch_millis = parse_datetime(s)
    if epoch_millis is None:
        return None
    dt = datetime.fromtimestamp(epoch_millis / 1000, tz=timezone.utc)
    # Recompute ms precisely to avoid float issues
    total_ms = epoch_millis
    seconds, ms = divmod(total_ms, 1000)
    dt2 = datetime.fromtimestamp(seconds, tz=timezone.utc)
    iso = dt2.strftime("%Y-%m-%dT%H:%M:%S") + "." + str(ms).zfill(3) + "Z"
    return iso, epoch_millis


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------

_WS_RE = re.compile(
    "[\\s\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000\uFEFF]+"
)


def canonicalize_string(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    s = s.strip()
    s = _WS_RE.sub(" ", s)
    return s


# ---------------------------------------------------------------------------
# Tokenization for Jaccard similarity
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
# Note: \w includes underscore and digits; we exclude underscore via [^\W_]
# This approximates "letter/number" unicode word chars.


def word_set(s: str) -> set:
    return set(_WORD_RE.findall(s))


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    inter = len(a & b)
    union = len(a) + len(b) - inter
    if union == 0:
        return 1.0
    return inter / union


# ---------------------------------------------------------------------------
# Compact JSON serialization with exact key order
# ---------------------------------------------------------------------------


def compact_row_json(row: dict) -> str:
    ordered = {
        "id": row["id"],
        "entity": row["entity"],
        "eventTime": row["eventTime"],
        "revision": row["revision"],
        "text": row["text"],
    }
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Generation / URI validation
# ---------------------------------------------------------------------------

_GEN_RE = re.compile(r"^[0-9]+$")


def is_valid_generation_string(g: Any) -> bool:
    return isinstance(g, str) and bool(_GEN_RE.match(g))


_URI_RE = re.compile(r"^gs://[^/]+/.+$")


def is_valid_uri(u: Any) -> bool:
    return isinstance(u, str) and bool(_URI_RE.match(u))


# ---------------------------------------------------------------------------
# Policy validation
# ---------------------------------------------------------------------------


def validate_policy(policy: Any):
    if not isinstance(policy, dict):
        return None
    min_time = policy.get("minTime")
    max_time = policy.get("maxTime")
    threshold = policy.get("contaminationThreshold")

    min_norm = normalize_datetime(min_time)
    max_norm = normalize_datetime(max_time)
    if min_norm is None or max_norm is None:
        return None

    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        return None
    if threshold != threshold or threshold in (float("inf"), float("-inf")):  # NaN / inf
        return None
    if threshold < 0 or threshold > 1:
        return None

    min_millis = min_norm[1]
    max_millis = max_norm[1]
    if min_millis > max_millis:
        return None

    return {"minMillis": min_millis, "maxMillis": max_millis, "threshold": float(threshold)}


# ---------------------------------------------------------------------------
# Row shape validation
# ---------------------------------------------------------------------------


def is_safe_nonneg_int(n: Any) -> bool:
    if isinstance(n, bool):
        return False
    if not isinstance(n, int):
        return False
    return n >= 0 and abs(n) <= 2**53 - 1


def validate_row_shape(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    expected = {"id", "entity", "eventTime", "revision", "text"}
    if set(row.keys()) != expected:
        return False
    if not isinstance(row["id"], str):
        return False
    if not isinstance(row["entity"], str):
        return False
    if not isinstance(row["eventTime"], str):
        return False
    if not isinstance(row["text"], str):
        return False
    if not is_safe_nonneg_int(row["revision"]):
        return False
    if parse_datetime(row["eventTime"]) is None:
        return False
    return True


# ---------------------------------------------------------------------------
# Bucket assignment
# ---------------------------------------------------------------------------


def bucket_for(entity: str) -> str:
    digest = hashlib.sha256(utf8_bytes(entity)).digest()
    first_byte = digest[0]
    b = first_byte % 10
    if b <= 5:
        return "train"
    if b <= 7:
        return "validation"
    return "test"


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------


def process_build_corpus(body: Any):
    if not isinstance(body, dict):
        return 400, {"error": "INVALID_INPUT"}

    policy = body.get("policy")
    objects = body.get("objects")

    if not isinstance(policy, dict) or not isinstance(objects, list):
        return 400, {"error": "INVALID_INPUT"}

    policy_result = validate_policy(policy)

    rejected_objects = []  # list of {"uri": ..., "reasonCodes": [...]}
    lineage = []
    accepted_rows = []  # dicts with id, entity, eventTime, epochMillis, revision, text
    rejected_rows_map: dict[str, set] = {}

    def add_row_rejection(row_id: str, code: str):
        rejected_rows_map.setdefault(row_id, set()).add(code)

    for obj in objects:
        codes = set()

        if not isinstance(obj, dict):
            rejected_objects.append({"uri": None, "reasonCodes": ["URI_INVALID"]})
            continue

        uri_val = obj.get("uri")
        uri_out = uri_val if isinstance(uri_val, str) else None

        if not is_valid_uri(obj.get("uri")):
            codes.add("URI_INVALID")

        gen = obj.get("generation")
        fetched_gen = obj.get("fetchedGeneration")
        gen_valid = is_valid_generation_string(gen)
        fetched_gen_valid = is_valid_generation_string(fetched_gen)
        if not gen_valid or not fetched_gen_valid:
            codes.add("GENERATION_INVALID")
        elif gen != fetched_gen:
            codes.add("GENERATION_MISMATCH")

        crc = obj.get("crc32c")
        crc_syntax_valid = isinstance(crc, str) and bool(re.match(r"^[0-9a-f]{8}$", crc))
        if not crc_syntax_valid:
            codes.add("CRC32C_INVALID")

        content = obj.get("content")
        content_is_string = isinstance(content, str)

        if crc_syntax_valid and content_is_string:
            actual = crc32c_hex(utf8_bytes(content))
            if actual != crc:
                codes.add("CRC32C_MISMATCH")

        if obj.get("schemaId") != "training-v1":
            codes.add("SCHEMA_INVALID")
        if not content_is_string:
            codes.add("SCHEMA_INVALID")

        parsed_rows = None
        jsonl_invalid = False
        schema_invalid_from_rows = False

        if content_is_string:
            lines = re.split(r"\r\n|\r|\n", content)
            rows = []
            any_nonblank = False
            for line in lines:
                if line.strip() == "":
                    continue
                any_nonblank = True
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    jsonl_invalid = True
                    continue
                if not validate_row_shape(parsed):
                    schema_invalid_from_rows = True
                    continue
                rows.append(parsed)
            if not jsonl_invalid:
                if not any_nonblank:
                    schema_invalid_from_rows = True
                parsed_rows = rows

        if jsonl_invalid:
            codes.add("JSONL_INVALID")
        if schema_invalid_from_rows:
            codes.add("SCHEMA_INVALID")

        if codes:
            rejected_objects.append({"uri": uri_out, "reasonCodes": list(codes)})
            continue

        lineage.append(
            {
                "uri": obj.get("uri"),
                "generation": obj.get("generation"),
                "crc32c": obj.get("crc32c"),
                "schemaId": obj.get("schemaId"),
            }
        )

        for row in parsed_rows:
            norm = normalize_datetime(row["eventTime"])
            iso, epoch_millis = norm
            accepted_rows.append(
                {
                    "id": row["id"],
                    "entity": canonicalize_string(row["entity"]),
                    "eventTime": iso,
                    "epochMillis": epoch_millis,
                    "revision": row["revision"],
                    "text": canonicalize_string(row["text"]),
                }
            )

    # ---------------- Deduplication ----------------
    groups: dict[str, list] = {}
    for row in accepted_rows:
        key = json.dumps([row["entity"], row["eventTime"], row["text"]], ensure_ascii=False)
        groups.setdefault(key, []).append(row)

    retained = []
    for rows in groups.values():
        if len(rows) == 1:
            retained.append(rows[0])
            continue
        winner = rows[0]
        for cand in rows[1:]:
            if cand["revision"] > winner["revision"]:
                winner = cand
            elif cand["revision"] == winner["revision"]:
                if compare_utf8_bytes(cand["id"], winner["id"]) < 0:
                    winner = cand
        for r in rows:
            if r is not winner:
                add_row_rejection(r["id"], "DUPLICATE")
        retained.append(winner)

    # ---------------- Policy check ----------------
    window_filtered = []
    if policy_result is None:
        for r in retained:
            add_row_rejection(r["id"], "POLICY_INVALID")
    else:
        for r in retained:
            if r["epochMillis"] < policy_result["minMillis"] or r["epochMillis"] > policy_result["maxMillis"]:
                add_row_rejection(r["id"], "OUT_OF_WINDOW")
            else:
                window_filtered.append(r)

    # ---------------- Bucket assignment ----------------
    buckets = {"train": [], "validation": [], "test": []}
    for r in window_filtered:
        b = bucket_for(r["entity"])
        r["bucket"] = b
        buckets[b].append(r)

    # ---------------- Contamination check ----------------
    train_word_sets = [(r, word_set(r["text"])) for r in buckets["train"]]

    def is_contaminated(row) -> bool:
        w = word_set(row["text"])
        for _, tw in train_word_sets:
            sim = jaccard(w, tw)
            if policy_result and sim >= policy_result["threshold"]:
                return True
        return False

    final_train = list(buckets["train"])
    final_validation = []
    final_test = []

    for r in buckets["validation"]:
        if is_contaminated(r):
            add_row_rejection(r["id"], "TRAIN_CONTAMINATION")
        else:
            final_validation.append(r)

    for r in buckets["test"]:
        if is_contaminated(r):
            add_row_rejection(r["id"], "TRAIN_CONTAMINATION")
        else:
            final_test.append(r)

    # ---------------- Sort splits ----------------
    def sort_rows(rows):
        return sorted(
            rows,
            key=lambda r: (utf8_sort_key(r["id"]), utf8_sort_key(compact_row_json(r))),
        )

    sorted_train = sort_rows(final_train)
    sorted_validation = sort_rows(final_validation)
    sorted_test = sort_rows(final_test)

    # ---------------- Serialize + hash ----------------
    def serialize_and_hash(rows) -> str:
        out = "".join(compact_row_json(r) + "\n" for r in rows)
        data = utf8_bytes(out)
        return hashlib.sha256(data).hexdigest()

    train_digest = serialize_and_hash(sorted_train)
    validation_digest = serialize_and_hash(sorted_validation)
    test_digest = serialize_and_hash(sorted_test)

    def to_output_row(r):
        return {
            "id": r["id"],
            "entity": r["entity"],
            "eventTime": r["eventTime"],
            "revision": r["revision"],
            "text": r["text"],
        }

    # ---------------- Assemble rejectedRows ----------------
    rejected_rows = []
    for row_id, code_set in rejected_rows_map.items():
        rejected_rows.append(
            {"id": row_id, "reasonCodes": sorted(code_set, key=utf8_sort_key)}
        )
    rejected_rows.sort(
        key=lambda r: (
            utf8_sort_key(r["id"]),
            utf8_sort_key(json.dumps(r, ensure_ascii=False, separators=(",", ":"))),
        )
    )

    # ---------------- Sort rejectedObjects / lineage ----------------
    def sort_by_uri_like(arr, key_name):
        def sort_key(item):
            v = item[key_name] if item[key_name] is not None else ""
            return (
                utf8_sort_key(str(v)),
                utf8_sort_key(json.dumps(item, ensure_ascii=False, separators=(",", ":"))),
            )

        return sorted(arr, key=sort_key)

    rejected_objects_out = [
        {"uri": o["uri"], "reasonCodes": sorted(set(o["reasonCodes"]), key=utf8_sort_key)}
        for o in rejected_objects
    ]
    sorted_rejected_objects = sort_by_uri_like(rejected_objects_out, "uri")
    sorted_lineage = sort_by_uri_like(lineage, "uri")

    response = {
        "splits": {
            "train": [to_output_row(r) for r in sorted_train],
            "validation": [to_output_row(r) for r in sorted_validation],
            "test": [to_output_row(r) for r in sorted_test],
        },
        "rejectedObjects": sorted_rejected_objects,
        "rejectedRows": rejected_rows,
        "digests": {
            "train": train_digest,
            "validation": validation_digest,
            "test": test_digest,
        },
        "lineage": sorted_lineage,
    }

    return 200, response


# ---------------------------------------------------------------------------
# FastAPI route
# ---------------------------------------------------------------------------


@app.post("/build-corpus")
async def build_corpus(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})

    status, payload = process_build_corpus(body)
    return JSONResponse(status_code=status, content=payload)