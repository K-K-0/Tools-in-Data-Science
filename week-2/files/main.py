import os
import redis
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


@app.post("/hit/{key}")
async def hit(key: str):
    count = r.incr(key)
    return JSONResponse(content={"key": key, "count": count})


@app.get("/count/{key}")
async def count(key: str):
    val = r.get(key)
    return JSONResponse(content={"key": key, "count": int(val) if val is not None else 0})


@app.get("/healthz")
async def healthz():
    try:
        r.ping()
        return JSONResponse(content={"status": "ok", "redis": "up"})
    except Exception:
        raise HTTPException(status_code=500, detail={"status": "error", "redis": "down"})
