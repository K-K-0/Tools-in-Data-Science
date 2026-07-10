"""
Multimodal QA API for scanned document extraction — using OpenRouter.

Endpoint: POST /answer-image
Request:  {"image_base64": "...", "question": "..."}
Response: {"answer": "..."}

Deploy with: uvicorn main:app --host 0.0.0.0 --port 8001
"""

import base64
import os
import re

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI

app = FastAPI(title="Multimodal Image QA API (OpenRouter)")

# --- CORS: required so the grader's Cloudflare Worker can call this endpoint ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- OpenRouter client setup ---
# Get a key at https://openrouter.ai/keys
# Set OPENROUTER_API_KEY as an environment variable / deployment secret.

BASE_URL = "https://openrouter.ai/api/v1"

# Free, vision-capable models currently available on OpenRouter (July 2026):
#   "google/gemma-4-31b-it:free"                       - strongest free vision option, 262K context
#   "google/gemma-4-26b-a4b-it:free"                   - lighter/faster MoE variant, also handles short video
#   "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free" - text/image/video/audio, reasoning-oriented
#   "openrouter/free"                                  - auto-router; picks any free model that supports
#                                                          the features your request needs (e.g. images)
# Free models are rate-limited (~20 req/min, ~200 req/day per OpenRouter account).
# Check https://openrouter.ai/models?modality=text%2Bimage-%3Etext for the live, current list -
# free-tier availability changes often, so verify before a graded run.
MODEL = os.environ.get("LLM_MODEL", "google/gemma-4-31b-it:free")

client = OpenAI(
    api_key=API_KEY,
    base_url=BASE_URL,
    # OpenRouter uses these optional headers for its analytics/leaderboard;
    # not required for the API to work, but good practice to set.
    default_headers={
        "HTTP-Referer": os.environ.get("APP_URL", "https://example.com"),
        "X-Title": "Invoice/Chart Image QA API",
    },
)

SYSTEM_PROMPT = (
    "You are a precise document-reading assistant. You will be shown an image "
    "(chart, receipt, invoice, table, or pie chart) and asked a question about it. "
    "If the question requires a calculation (e.g. summing values across a chart), "
    "perform that calculation yourself using the values visible in the image. "
    "Answer with ONLY the direct answer - no explanation, no extra words. "
    "If the answer is a number, output only the number itself with no currency "
    "symbols, no units, no commas as thousands separators, and no percent signs."
)


class AnswerImageRequest(BaseModel):
    image_base64: str
    question: str


class AnswerImageResponse(BaseModel):
    answer: str


def clean_numeric_answer(text: str) -> str:
    """
    If the model's answer is purely numeric (possibly with $ , % etc.),
    strip currency symbols, commas, units, and whitespace, keeping just
    the number. If the answer isn't numeric, return it stripped as-is.
    """
    stripped = text.strip()

    match = re.search(r"-?\d[\d,]*\.?\d*", stripped)
    if match:
        candidate = match.group(0).replace(",", "")
        digits_only = re.sub(r"[^\d]", "", stripped)
        if digits_only and len(re.sub(r"[^\d]", "", candidate)) >= len(digits_only) * 0.8:
            return candidate

    return stripped


def normalize_base64(image_base64: str) -> str:
    """Strip data-URL prefix if present, e.g. 'data:image/png;base64,...'."""
    if image_base64.startswith("data:"):
        return image_base64.split(",", 1)[-1]
    return image_base64


@app.post("/answer-image", response_model=AnswerImageResponse)
def answer_image(req: AnswerImageRequest):
    if not req.image_base64 or not req.question:
        raise HTTPException(status_code=400, detail="image_base64 and question are required")

    img_data = normalize_base64(req.image_base64)

    try:
        base64.b64decode(img_data, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="image_base64 is not valid base64")

    # Fallback chain: try the configured model first, then other free vision
    # models in case of rate limiting or transient provider issues.
    fallback_models = [MODEL, "google/gemma-4-26b-a4b-it:free", "openrouter/free"]
    seen = set()
    models_to_try = [m for m in fallback_models if not (m in seen or seen.add(m))]

    last_error = None
    raw_answer = None

    for model_name in models_to_try:
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": req.question},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{img_data}"},
                            },
                        ],
                    },
                ],
                temperature=0,
                max_tokens=100,
            )
            raw_answer = response.choices[0].message.content.strip()
            break  # success, stop trying further models
        except Exception as e:
            last_error = e
            continue

    if raw_answer is None:
        raise HTTPException(status_code=502, detail=f"All LLM calls failed: {last_error}")

    return AnswerImageResponse(answer=clean_numeric_answer(raw_answer))


@app.get("/")
def health_check():
    return {"status": "ok"}