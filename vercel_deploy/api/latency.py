import json
import numpy as np
import pandas as pd
from pathlib import Path
from http.server import BaseHTTPRequestHandler

DATA_PATH = Path(__file__).parent.parent / "data" / "latency.csv"
df = pd.read_json(DATA_PATH)

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Max-Age": "86400",
}

class handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        for k, v in CORS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        self._send(200, {"status": "ok"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
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
            self._send(200, result)
        except Exception as e:
            self._send(500, {"error": str(e)})