"""
Invoice Extraction API.

Endpoint: POST /extract
Request:  {"document_id": "...", "text": "...", "schema": {...}}
Response: exact JSON matching the extraction rules (see system prompt).

Deploy with: uvicorn main:app --host 0.0.0.0 --port 8001
"""

import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional
from openai import OpenAI

app = FastAPI(title="Invoice Extraction API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_KEY = os.environ["LLM_API_KEY"]
BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

SYSTEM_PROMPT = """You extract structured data from messy free-text invoices for an ERP system.
Follow these rules EXACTLY:

- vendor: the biller's proper name, exactly as written in the text.
- currency: ISO 4217 code (USD, EUR, GBP, INR, JPY). Infer from symbols/words:
  "$" or "dollars" -> USD, "\u20ac" or "euros" -> EUR, "\u00a3" or "pounds sterling" -> GBP,
  "\u20b9" or "rupees" -> INR, "\u00a5" or "yen" -> JPY.
- total_amount: integer in the MAIN unit, no separators, no symbols, no decimals.
  Handle: spelled-out numbers ("twelve thousand four hundred eighty" -> 12480),
  standard grouping ("12,480" -> 12480), Indian grouping ("1,24,800" -> 124800),
  and "K" suffix ("12K" -> 12000).
- invoice_date: normalize to YYYY-MM-DD regardless of input format.
- due_in_days: integer. Parse phrasing like "Net 30" -> 30, "payable within 45 days" -> 45,
  "due in two weeks" -> 14, "due in one month" -> 30.
- is_paid: boolean. "paid in full", "payment received" -> true. "awaiting payment",
  "outstanding", "unpaid", no payment mention -> false.
- priority: one of low, normal, high, urgent -- infer from tone/wording/deadlines if not stated explicitly.
  Default to "normal" if there is no signal either way.
- contact_email: lowercased exactly as it appears (just lowercase the casing, don't alter the address).
- line_items: array of {sku, quantity, unit_price} IN THE ORDER they appear in the text.
  unit_price is an integer (no decimals/symbols).
- item_count: integer count of line_items.

Return ONLY the requested JSON object. No explanation, no markdown fences, no extra keys,
no missing keys. Every key must be present even if you have to make a reasonable inference."""


class ExtractRequest(BaseModel):
    document_id: str
    text: str
    schema_: Optional[Dict[str, Any]] = Field(default=None, alias="schema")

    class Config:
        populate_by_name = True


# Fallback schema matching the spec, used only if the request doesn't include one.
DEFAULT_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor": {"type": "string"},
        "currency": {"type": "string"},
        "total_amount": {"type": "integer"},
        "invoice_date": {"type": "string"},
        "due_in_days": {"type": "integer"},
        "is_paid": {"type": "boolean"},
        "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
        "contact_email": {"type": "string"},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "unit_price": {"type": "integer"},
                },
                "required": ["sku", "quantity", "unit_price"],
                "additionalProperties": False,
            },
        },
        "item_count": {"type": "integer"},
    },
    "required": [
        "vendor", "currency", "total_amount", "invoice_date", "due_in_days",
        "is_paid", "priority", "contact_email", "line_items", "item_count",
    ],
    "additionalProperties": False,
}


def strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    return t.strip()


@app.post("/extract")
def extract(req: ExtractRequest):
    if not req.text or not req.text.strip():
        raise HTTPException(status_code=400, detail="text is required")

    json_schema = req.schema_ if req.schema_ else DEFAULT_SCHEMA

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": req.text},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "invoice_extraction",
                    "schema": json_schema,
                    "strict": True,
                },
            },
            temperature=0,
        )
        raw = response.choices[0].message.content
        result = json.loads(strip_fences(raw))
    except Exception:
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT + "\n\nSchema to follow:\n" + json.dumps(json_schema)},
                    {"role": "user", "content": req.text},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            raw = response.choices[0].message.content
            result = json.loads(strip_fences(raw))
        except Exception as e2:
            raise HTTPException(status_code=502, detail=f"LLM extraction failed: {e2}")

    return result


@app.get("/")
def health_check():
    return {"status": "ok"}