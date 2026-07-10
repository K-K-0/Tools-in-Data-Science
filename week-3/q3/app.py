"""
Invoice Field Extraction API.

Endpoint: POST /extract
Request:  {"invoice_text": "..."}
Response: {"invoice_no": ..., "date": ..., "vendor": ..., "amount": ..., "tax": ..., "currency": ...}

Deploy with: uvicorn main:app --host 0.0.0.0 --port 8001
"""

import json
import os
import re

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI

app = FastAPI(title="Invoice Extraction API")

# --- CORS: required so the grader's Cloudflare Worker can call this endpoint ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- LLM client setup ---
# Works with OpenAI directly, or an OpenAI-compatible proxy (e.g. AIPipe).
API_KEY =  "eyJhbGciOiJIUzI1NiJ9.eyJlbWFpbCI6IjI0ZjEwMDIwNTJAZHMuc3R1ZHkuaWl0bS5hYy5pbiIsImlhdCI6MTc4MzYwMDU4OSwiaXNzIjoiaHR0cHM6Ly9haXBpcGUub3JnIiwiYXVkIjoiYWlwaXBlLWFwaSIsImV4cCI6MTc4NDIwNTM4OX0.Xmc7y2c3z2gIxHyTdKeSGDUKRkk351Dr8NO_Q86fx04"
BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

FIELDS = ["invoice_no", "date", "vendor", "amount", "tax", "currency"]

SYSTEM_PROMPT = (
    "You extract structured fields from raw invoice text that may come in many "
    "different layouts and formats. Extract exactly these six fields:\n"
    "- invoice_no: the invoice number/ID as a string (e.g. 'INV-2026-0041')\n"
    "- date: the invoice date, converted to ISO format YYYY-MM-DD\n"
    "- vendor: the vendor/seller/company name as a string\n"
    "- amount: the SUBTOTAL before tax, as a plain number (no currency symbols, no commas)\n"
    "- tax: the tax amount only (e.g. GST, VAT), as a plain number (no currency symbols, no commas)\n"
    "- currency: the 3-letter ISO currency code (e.g. INR, USD, EUR). Infer from symbols "
    "like 'Rs.' or '₹' -> INR, '$' -> USD, '€' -> EUR, '£' -> GBP if not stated explicitly.\n\n"
    "Respond with ONLY a single JSON object with exactly these six keys, nothing else, "
    "no markdown code fences, no explanation. If a field cannot be found in the text, "
    "use JSON null for that field (not the string \"null\"). "
    "amount and tax must be JSON numbers, not strings, when present."
)


class ExtractRequest(BaseModel):
    invoice_text: str


class ExtractResponse(BaseModel):
    invoice_no: str | None = None
    date: str | None = None
    vendor: str | None = None
    amount: float | None = None
    tax: float | None = None
    currency: str | None = None


def regex_fallback(text: str) -> dict:
    """
    Best-effort regex extraction, used to fill in any fields the LLM
    response is missing or if the LLM call fails outright.
    """
    result = {f: None for f in FIELDS}

    m = re.search(r"invoice\s*(?:no|number|#)?\.?\s*:?\s*([A-Za-z0-9\-\/]+)", text, re.I)
    if m:
        result["invoice_no"] = m.group(1).strip()

    m = re.search(r"date\s*:?\s*([0-9]{1,2}\s+\w+\s+[0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}[\/\-][0-9]{1,2}[\/\-][0-9]{2,4})", text, re.I)
    if m:
        result["date"] = m.group(1).strip()  # left as-is; ISO normalization is the LLM's job

    m = re.search(r"vendor\s*:?\s*(.+)", text, re.I)
    if m:
        result["vendor"] = m.group(1).strip().split("\n")[0]

    m = re.search(r"sub\s*-?\s*total\s*:?\s*(?:rs\.?|inr|\$|₹|usd)?\s*([\d,]+\.?\d*)", text, re.I)
    if m:
        result["amount"] = float(m.group(1).replace(",", ""))

    m = re.search(r"(?:gst|vat|tax)[^:]*:?\s*(?:rs\.?|inr|\$|₹|usd)?\s*([\d,]+\.?\d*)", text, re.I)
    if m:
        result["tax"] = float(m.group(1).replace(",", ""))

    if re.search(r"rs\.?|inr|₹", text, re.I):
        result["currency"] = "INR"
    elif re.search(r"\$|usd", text, re.I):
        result["currency"] = "USD"
    elif re.search(r"€|eur", text, re.I):
        result["currency"] = "EUR"
    elif re.search(r"£|gbp", text, re.I):
        result["currency"] = "GBP"

    return result


def merge_with_fallback(llm_result: dict, text: str) -> dict:
    """Fill any null/missing LLM fields with regex fallback values."""
    fallback = regex_fallback(text)
    merged = {}
    for f in FIELDS:
        val = llm_result.get(f)
        merged[f] = val if val not in (None, "", "null") else fallback.get(f)
    return merged


def parse_llm_json(raw: str) -> dict:
    """Strip markdown code fences if present, then parse JSON."""
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


@app.post("/extract", response_model=ExtractResponse)
def extract(req: ExtractRequest):
    if not req.invoice_text or not req.invoice_text.strip():
        raise HTTPException(status_code=400, detail="invoice_text is required")

    llm_result = {f: None for f in FIELDS}

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": req.invoice_text},
            ],
            temperature=0,
            max_tokens=300,
        )
        raw = response.choices[0].message.content
        parsed = parse_llm_json(raw)
        for f in FIELDS:
            if f in parsed:
                llm_result[f] = parsed[f]
    except Exception:
        # LLM call or parsing failed entirely; fall through to pure regex fallback
        pass

    final = merge_with_fallback(llm_result, req.invoice_text)

    # Type safety: ensure amount/tax are floats or None
    for key in ("amount", "tax"):
        if final[key] is not None:
            try:
                final[key] = float(final[key])
            except (TypeError, ValueError):
                final[key] = None

    return ExtractResponse(**final)


@app.get("/")
def health_check():
    return {"status": "ok"}