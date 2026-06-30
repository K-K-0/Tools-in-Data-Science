import time
import uuid
import json
from collections import deque
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

YOUR_EMAIL = "24f1002052@ds.study.iitm.ac.in"  # <-- replace with your actual email

START_TIME = time.time()
REQUEST_COUNT = 0
LOG_BUFFER = deque(maxlen=1000)


def log_entry(level: str, path: str, request_id: str, **extra):
    entry = {
        "level": level,
        "ts": time.time(),
        "path": path,
        "request_id": request_id,
        **extra,
    }
    LOG_BUFFER.append(entry)
    return entry


@app.middleware("http")
async def count_and_log_requests(request: Request, call_next):
    global REQUEST_COUNT
    REQUEST_COUNT += 1
    request_id = str(uuid.uuid4())
    log_entry("info", request.url.path, request_id, method=request.method)
    response = await call_next(request)
    return response


@app.get("/work")
async def work(n: int = 1):
    # Do K units of "work" - simple busy loop simulation
    total = 0
    for i in range(max(0, n)):
        total += i
    return {"email": YOUR_EMAIL, "done": n}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    body = (
        "# HELP http_requests_total Total number of HTTP requests\n"
        "# TYPE http_requests_total counter\n"
        f"http_requests_total {REQUEST_COUNT}\n"
    )
    return PlainTextResponse(content=body, media_type="text/plain; version=0.0.4")


@app.get("/healthz")
async def healthz():
    uptime = time.time() - START_TIME
    return {"status": "ok", "uptime_s": uptime}


@app.get("/logs/tail")
async def logs_tail(limit: int = 50):
    limit = max(0, limit)
    entries = list(LOG_BUFFER)[-limit:] if limit > 0 else []
    return JSONResponse(content=entries)