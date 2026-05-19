import sys
import traceback
import os
from io import StringIO
from typing import List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI

# ── App setup ────────────────────────────────────────────────────────────────

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── OpenAI-compatible client pointing at aipipe ───────────────────────────────

client = OpenAI(
    api_key=os.environ.get("AIPIPE_TOKEN"),       # your aipipe token
    base_url="https://aipipe.org/openrouter/v1",  # or /openai/v1
)

# ── Pydantic models ───────────────────────────────────────────────────────────

class CodeRequest(BaseModel):
    code: str

class CodeResponse(BaseModel):
    error: List[int]
    result: str

class ErrorAnalysis(BaseModel):
    error_lines: List[int]

# ── Tool: execute Python code ─────────────────────────────────────────────────

def execute_python_code(code: str) -> dict:
    """
    Execute Python code and return exact output.

    Returns:
        {"success": bool, "output": str}  # exact stdout or full traceback
    """
    old_stdout = sys.stdout
    sys.stdout = StringIO()

    try:
        exec(code, {})  # isolated globals
        output = sys.stdout.getvalue()
        return {"success": True, "output": output}

    except Exception:
        output = traceback.format_exc()
        return {"success": False, "output": output}

    finally:
        sys.stdout = old_stdout

# ── AI agent: identify error line numbers ────────────────────────────────────

def analyze_error_with_ai(code: str, tb: str) -> List[int]:
    """
    Use LLM with structured output to identify error line numbers.
    Only called when execution fails.
    """
    prompt = f"""Analyze this Python code and its error traceback.
Identify the line number(s) in the user's code where the error occurred.

CODE:
{code}

TRACEBACK:
{tb}

Return ONLY the line number(s) where the error is located in the user's code.
"""

    response = client.beta.chat.completions.parse(
        model="openai/gpt-4.1-nano",   # cheap and fast; works for this task
        messages=[{"role": "user", "content": prompt}],
        response_format=ErrorAnalysis,
    )

    result: ErrorAnalysis = response.choices[0].message.parsed
    return result.error_lines

# ── Endpoint ──────────────────────────────────────────────────────────────────

@app.post("/code-interpreter", response_model=CodeResponse)
async def code_interpreter(request: CodeRequest):
    # Step 1: run the code
    execution = execute_python_code(request.code)

    # Step 2: success path — no AI needed
    if execution["success"]:
        return CodeResponse(error=[], result=execution["output"])

    # Step 3: error path — ask AI for line numbers
    error_lines = analyze_error_with_ai(request.code, execution["output"])

    return CodeResponse(error=error_lines, result=execution["output"])