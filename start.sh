#!/bin/bash
set -e

echo "[STARTUP] Starting PO Token server..."
node /opt/pot-provider/server/build/main.js --port 4416 &
POT_PID=$!

sleep 3

if kill -0 $POT_PID 2>/dev/null; then
    echo "[STARTUP] PO Token server running on port 4416 (PID: $POT_PID)"
else
    echo "[STARTUP] WARNING: PO Token server failed to start"
fi

echo "[STARTUP] Starting Gogu music bot..."

# Verify PO Token server is responding
if command -v curl &> /dev/null; then
    POT_TEST=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:4416/token 2>/dev/null || echo "failed")
    echo "[STARTUP] PO Token server health check: $POT_TEST"
fi

exec python bot.py
