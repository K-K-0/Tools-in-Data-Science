#!/bin/bash
# Run this script on your local machine to start everything

set -e

echo "=== Starting docker compose stack ==="
docker compose up --build -d

echo "=== Waiting for API to be ready ==="
until curl -sf http://localhost:8000/healthz > /dev/null; do
  echo "  waiting..."
  sleep 2
done
echo "  API is up!"

echo ""
echo "=== Choose a tunnel method ==="
echo ""
echo "Option 1 — cloudflared (recommended, stable URL):"
echo "  brew install cloudflare/cloudflare/cloudflared   # mac"
echo "  cloudflared tunnel --url http://localhost:8000"
echo ""
echo "Option 2 — localtunnel (no install needed):"
echo "  npx localtunnel --port 8000"
echo ""
echo "Option 3 — ngrok:"
echo "  ngrok http 8000"
echo ""
echo "Paste the tunnel URL into the grader."
