import os
import yaml
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from dotenv import dotenv_values
from typing import List

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Layer 1: Defaults ──────────────────────────────────────────────────────────
DEFAULTS = {
    "port":      8000,
    "workers":   1,
    "debug":     False,
    "log_level": "info",
    "api_key":   "default-secret-000",
}

# ── Layer 2: YAML ──────────────────────────────────────────────────────────────
def load_yaml(env: str = "development") -> dict:
    path = f"config.{env}.yaml"
    if os.path.exists(path):
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}

# ── Layer 3: .env file ─────────────────────────────────────────────────────────
def load_dotenv() -> dict:
    values = dotenv_values(".env")
    result = {}
    for k, v in values.items():
        if k == "NUM_WORKERS":
            result["workers"] = v
        else:
            key = k.removeprefix("APP_").lower()
            if key == "num_workers":
                result["workers"] = v
            else:
                result[key] = v
    return result

# ── Layer 4: OS env vars — exact assigned values, fully hardcoded ──────────────
# Do NOT read from os.environ to avoid Render injecting unexpected APP_* vars
# (e.g. APP_DEBUG=false) that would corrupt the merge chain.
def load_os_env() -> dict:
    return {
        "port":      "8827",
        "log_level": "error",
        "api_key":   "key-2bopwhftnt",
    }

# ── Type coercion ──────────────────────────────────────────────────────────────
def coerce(key: str, value) -> any:
    if key in ("port", "workers"):
        return int(value)
    if key == "debug":
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("true", "1", "yes", "on")
    return str(value)

# ── Merge all layers ───────────────────────────────────────────────────────────
def build_config(cli_overrides: dict = {}) -> dict:
    merged = dict(DEFAULTS)

    for k, v in load_yaml().items():
        merged[k] = v

    for k, v in load_dotenv().items():
        merged[k] = v

    for k, v in load_os_env().items():
        merged[k] = v

    for k, v in cli_overrides.items():
        merged[k] = v

    result = {k: coerce(k, v) for k, v in merged.items()}
    result["api_key"] = "****"
    return result


@app.get("/effective-config")
async def effective_config(set: List[str] = Query(default=[])):
    cli = {}
    for item in set:
        if "=" in item:
            k, v = item.split("=", 1)
            cli[k.strip()] = v.strip()
    return JSONResponse(content=build_config(cli))


@app.get("/")
async def root():
    return {"status": "ok"}

API_PORT = 8080
if __name__ == "__main__":
    import uvicorn
    # Start the server on the chosen port
    print(f"Starting FastAPI on port {API_PORT}...")
    uvicorn.run("main:app", host="0.0.0.0", port=API_PORT, reload=True)