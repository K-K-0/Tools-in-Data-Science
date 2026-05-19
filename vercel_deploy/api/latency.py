import json

from httplib2 import Response
import numpy as np
import pandas as pd
from pathlib import Path

DATA_PATH = Path(__file__).parent.parent / "data" / "latency.json"
df = pd.read_json(DATA_PATH)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}

def handler(request):
    if request.method == "OPTIONS":
        return Response("", headers=CORS_HEADERS, status=200)

    body = request.json()
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

    return Response(
        json.dumps(result),
        headers={**CORS_HEADERS, "Content-Type": "application/json"},
        status=200
    )