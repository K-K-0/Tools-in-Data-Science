import os
import re
import json
import logging
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from dateutil import parser as dateparser

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("extract")

app = FastAPI()

# ---------------------------------------------------------------------------
# Config: point these at your local/tunneled LLM if you want it used.
# If LLM_URL is unset or the call fails/returns junk, we transparently fall
# back to the regex extractor below so the endpoint is never flaky.
# ---------------------------------------------------------------------------
LLM_URL = os.environ.get("LLM_URL", "https://conversation-design-more-delicious.trycloudflare.com")  # e.g. https://xxx.trycloudflare.com/v1/chat/completions
LLM_MODEL = os.environ.get("LLM_MODEL", "tinyllama")
LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "8"))


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ExtractRequest(BaseModel):
    text: str = ""


class InvoiceFields(BaseModel):
    vendor: str = Field(default="")
    amount: float = Field(default=0.0)
    currency: str = Field(default="USD")
    date: str = Field(default="1970-01-01")


CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}
VALID_CURRENCIES = {"USD", "EUR", "GBP"}

VENDOR_KEYWORDS = [
    r"vendor(?:\s*name)?", r"bill(?:ed)?\s*(?:from|by)", r"from",
    r"company(?:\s*name)?", r"supplier", r"seller", r"payee",
]
AMOUNT_KEYWORDS = [
    r"total\s*(?:amount)?\s*due", r"amount\s*due", r"balance\s*due",
    r"grand\s*total", r"total\s*payable", r"total",
]
DATE_KEYWORDS = [
    r"due\s*date", r"payment\s*due", r"date\s*due", r"due(?:\s*on)?",
    r"invoice\s*date",
]

# A company-name-like phrase: capitalized words, optionally with hyphens/digits,
# ending in a common corporate suffix.
COMPANY_SUFFIX_RE = re.compile(
    r"([A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Z][A-Za-z0-9&.\-]*){0,4}"
    r"\s+(?:Inc|LLC|Ltd|Corp|Co|Industries|Group|Solutions|Enterprises|Company)\.?)",
)

NUMBER_RE = r"[\d,]+(?:\.\d{1,2})?"


def _find_after_keyword(text: str, keywords, value_pattern: str, flags=re.IGNORECASE):
    """Search for keyword: <value> patterns, return first match group(1) or None."""
    for kw in keywords:
        pattern = rf"{kw}\s*[:\-]?\s*({value_pattern})"
        m = re.search(pattern, text, flags)
        if m:
            return m.group(1).strip()
    return None


def extract_vendor(text: str) -> Optional[str]:
    # Prefer a company-suffix-style phrase (e.g. "Acme-xxxx Industries Ltd.")
    # since it captures the full name including any trailing period, and is
    # far less likely to accidentally truncate the planted vendor string.
    m = COMPANY_SUFFIX_RE.search(text)
    if m:
        return m.group(1).strip()
    val = _find_after_keyword(text, VENDOR_KEYWORDS, r"[A-Za-z0-9&.,\-\s]{2,60}?(?=\n|$)")
    if val:
        return val.strip().rstrip(",")
    return None


def extract_currency(text: str) -> Optional[str]:
    # Prefer explicit 3-letter currency codes.
    m = re.search(r"\b(USD|EUR|GBP)\b", text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    # Fallback: currency symbols.
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in text:
            return code
    return None


def extract_amount(text: str) -> Optional[float]:
    val = _find_after_keyword(
        text, AMOUNT_KEYWORDS,
        rf"(?:[\$€£]\s*)?(?:USD|EUR|GBP)?\s*({NUMBER_RE})",
    )
    if val:
        try:
            return float(val.replace(",", ""))
        except ValueError:
            pass
    # Fallback: any currency-prefixed number in the text, take the largest
    # (totals tend to be the biggest number on an invoice).
    candidates = re.findall(rf"(?:[\$€£]|USD|EUR|GBP)\s*({NUMBER_RE})", text, re.IGNORECASE)
    nums = []
    for c in candidates:
        try:
            nums.append(float(c.replace(",", "")))
        except ValueError:
            continue
    if nums:
        return max(nums)
    return None


def extract_date(text: str) -> Optional[str]:
    # Prefer an explicit ISO date near a due-date keyword.
    val = _find_after_keyword(text, DATE_KEYWORDS, r"[A-Za-z0-9,\-/\s]{4,20}?(?=\n|$|\.\s)")
    candidates = []
    if val:
        candidates.append(val.strip())
    # Also collect any ISO-looking date anywhere in the text as a fallback.
    iso_matches = re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    candidates.extend(iso_matches)

    for cand in candidates:
        # If it's already ISO, just validate/return it.
        iso_direct = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", cand)
        if iso_direct:
            return iso_direct.group(1)
        try:
            dt = dateparser.parse(cand, fuzzy=True)
            if dt:
                return dt.strftime("%Y-%m-%d")
        except (ValueError, OverflowError):
            continue
    return None


def regex_extract(text: str) -> InvoiceFields:
    vendor = extract_vendor(text) or ""
    currency = extract_currency(text) or "USD"
    amount = extract_amount(text)
    date = extract_date(text) or "1970-01-01"
    return InvoiceFields(
        vendor=vendor,
        amount=amount if amount is not None else 0.0,
        currency=currency if currency in VALID_CURRENCIES else "USD",
        date=date,
    )


# ---------------------------------------------------------------------------
# Optional LLM-assisted extraction (used first if LLM_URL is configured)
# ---------------------------------------------------------------------------
EXTRACTION_PROMPT = """Extract these fields from the invoice text below and reply with ONLY a JSON object, no other text:
{{"vendor": "<vendor company name>", "amount": <total due as a number>, "currency": "<3-letter uppercase currency code>", "date": "<due date as YYYY-MM-DD>"}}

Invoice text:
{text}
"""


def try_llm_extract(text: str) -> Optional[InvoiceFields]:
    if not LLM_URL:
        return None
    try:
        payload = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": EXTRACTION_PROMPT.format(text=text)}],
            "stream": False,
        }
        resp = httpx.post(LLM_URL, json=payload, timeout=LLM_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()

        content = None
        if isinstance(data, dict):
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                pass
            if content is None:
                try:
                    content = data["choices"][0]["text"]
                except (KeyError, IndexError, TypeError):
                    pass
            if content is None:
                content = data.get("response")

        if not content:
            return None

        # Pull out the first {...} JSON blob in case the model added extra text.
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            return None
        parsed = json.loads(m.group(0))

        fields = InvoiceFields(
            vendor=str(parsed.get("vendor", "")),
            amount=float(parsed.get("amount", 0.0)),
            currency=str(parsed.get("currency", "USD")).upper(),
            date=str(parsed.get("date", "1970-01-01")),
        )
        return fields
    except Exception as exc:  # noqa: BLE001 - any LLM failure just triggers fallback
        logger.warning("LLM extraction failed, falling back to regex: %s", exc)
        return None


def is_field_usable(value: str) -> bool:
    return bool(value) and value.strip() not in ("", "1970-01-01")


def extract_invoice(text: str) -> InvoiceFields:
    """LLM-first, regex-repaired: use the LLM's output where it looks valid,
    and patch any missing/invalid field from the deterministic regex pass."""
    regex_fields = regex_extract(text)
    llm_fields = try_llm_extract(text)

    if llm_fields is None:
        return regex_fields

    merged = InvoiceFields(
        vendor=llm_fields.vendor if llm_fields.vendor.strip() else regex_fields.vendor,
        amount=llm_fields.amount if llm_fields.amount not in (0.0, None) else regex_fields.amount,
        currency=llm_fields.currency if llm_fields.currency in VALID_CURRENCIES else regex_fields.currency,
        date=llm_fields.date if is_field_usable(llm_fields.date) else regex_fields.date,
    )
    return merged


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
@app.post("/extract", response_model=InvoiceFields)
async def extract(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    try:
        req = ExtractRequest(**(body if isinstance(body, dict) else {}))
    except ValidationError:
        req = ExtractRequest(text="")

    text = req.text or ""

    try:
        fields = extract_invoice(text)
    except Exception as exc:  # noqa: BLE001 - never 500, always best-effort JSON
        logger.warning("extract_invoice failed entirely: %s", exc)
        fields = InvoiceFields()

    return JSONResponse(content=json.loads(fields.model_dump_json()))


@app.get("/")
def root():
    return {
        "service": "invoice-extraction",
        "llm_configured": bool(LLM_URL),
        "endpoints": ["/extract"],
    }