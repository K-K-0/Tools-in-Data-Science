from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import numpy as np
import pandas as pd
import json
from pathlib import Path

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_PATH = Path(__file__).parent.parent / "data" / "latency.csv"
df = pd.read_json(DATA_PATH)

@app.get("/")
def root():
    return {"status": "ok"}

@app.post("/api/latency")
async def latency(request: Request):
    body = await request.json()
    regions = body.get("regions", [])
    threshold = body.get("threshold_ms", 200)

    result = {}
    for region in regions:
        rdf = df[df["region"].str.lower() == region.lower()]
        if rdf.empty:
            result[region] = None
            continue
        result[region] = {
            "avg_latency": round(float(rdf["latency_ms"].mean()), 4),
            "p95_latency": round(float(np.percentile(rdf["latency_ms"], 95)), 4),
            "avg_uptime":  round(float(rdf["uptime_pct"].mean()), 4),
            "breaches":    int((rdf["latency_ms"] > threshold).sum()),
        }
    return result