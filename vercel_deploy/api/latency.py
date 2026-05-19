import json, numpy as np, pandas as pd
from pathlib import Path
from http.server import BaseHTTPRequestHandler

try:
    DATA_PATH = Path(__file__).parent.parent / "data" / "latency.json"
    df = pd.read_json(DATA_PATH)
except Exception as e:
    df = None
    LOAD_ERROR = str(e)

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        try:
            if df is None:
                raise RuntimeError(f"Data load failed: {LOAD_ERROR}")

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

            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        except Exception as e:
            self.send_response(500)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")