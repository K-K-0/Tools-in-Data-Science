import time
import json
import uuid
import threading
from collections import deque
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
EMAIL = "24f1002052@ds.study.iitm.ac.in"  # TODO: replace with your actual email
LOG_BUFFER_SIZE = 1000

app = FastAPI()
START_TIME = time.monotonic()

# ---------------------------------------------------------------------------
# Metrics (thread-safe, in-memory Prometheus-style counter)
# ---------------------------------------------------------------------------
_metrics_lock = threading.Lock()
_total_requests = 0
_request_counts = {}  # (method, path, status) -> count


def _incr_metric(method: str, path: str, status: int):
    global _total_requests
    key = (method, path, str(status))
    with _metrics_lock:
        _total_requests += 1
        _request_counts[key] = _request_counts.get(key, 0) + 1


def render_prometheus_text() -> str:
    lines = [
        "# HELP http_requests_total Total number of HTTP requests.",
        "# TYPE http_requests_total counter",
    ]
    with _metrics_lock:
        total = _total_requests
        items = list(_request_counts.items())

    # Plain unlabeled total first, so naive parsers (single-value regex) work.
    lines.append(f"http_requests_total {total}")

    # Labeled breakdown as a separate metric name for extra detail.
    lines.append("# HELP http_requests_total_by_label Requests broken down by method/path/status.")
    lines.append("# TYPE http_requests_total_by_label counter")
    if not items:
        lines.append('http_requests_total_by_label{method="none",path="none",status="0"} 0')
    else:
        for (method, path, status), count in items:
            lines.append(
                f'http_requests_total_by_label{{method="{method}",path="{path}",status="{status}"}} {count}'
            )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Structured logging (in-memory ring buffer of JSON log entries)
# ---------------------------------------------------------------------------
_log_lock = threading.Lock()
_log_buffer = deque(maxlen=LOG_BUFFER_SIZE)


def log_event(level: str, path: str, request_id: str, **extra):
    entry = {
        "level": level,
        "ts": datetime.now(timezone.utc).isoformat(),
        "path": path,
        "request_id": request_id,
    }
    entry.update(extra)
    with _log_lock:
        _log_buffer.append(entry)
    # Also print to stdout so logs are visible in platform log viewers.
    print(json.dumps(entry), flush=True)


# ---------------------------------------------------------------------------
# Middleware: instruments every request (counter + structured log)
# ---------------------------------------------------------------------------
@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    start = time.monotonic()

    log_event(
        level="INFO",
        path=request.url.path,
        request_id=request_id,
        event="request_started",
        method=request.method,
    )

    try:
        response = await call_next(request)
    except Exception as exc:
        log_event(
            level="ERROR",
            path=request.url.path,
            request_id=request_id,
            event="request_failed",
            method=request.method,
            error=str(exc),
        )
        _incr_metric(request.method, request.url.path, 500)
        raise

    duration_ms = (time.monotonic() - start) * 1000
    _incr_metric(request.method, request.url.path, response.status_code)

    log_event(
        level="INFO",
        path=request.url.path,
        request_id=request_id,
        event="request_completed",
        method=request.method,
        status=response.status_code,
        duration_ms=round(duration_ms, 3),
    )

    response.headers["X-Request-ID"] = request_id
    return response


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/work")
def do_work(n: int = 1):
    """Do K units of (fake) CPU work and return a result."""
    k = max(0, n)
    total = 0.0
    for i in range(k):
        # trivial busy-work so the endpoint takes non-zero time for larger n
        total += (i * i) % 97

    return {"email": EMAIL, "done": k}


@app.get("/metrics")
def metrics():
    return PlainTextResponse(render_prometheus_text(), media_type="text/plain; version=0.0.4")


@app.get("/healthz")
def healthz():
    uptime_s = max(0.0, time.monotonic() - START_TIME)
    return JSONResponse({"status": "ok", "uptime_s": uptime_s})


@app.get("/logs/tail")
def logs_tail(limit: int = 50):
    limit = max(0, limit)
    with _log_lock:
        entries = list(_log_buffer)[-limit:] if limit > 0 else []
    return JSONResponse(entries)


@app.get("/")
def root():
    return {"service": "observability-demo", "endpoints": ["/work", "/metrics", "/healthz", "/logs/tail"]}