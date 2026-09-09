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

# Verify Deno is available (required for yt-dlp JS challenges)
if command -v deno &> /dev/null; then
    echo "[STARTUP] Deno available: $(deno --version | head -1)"
else
    echo "[STARTUP] WARNING: Deno not found — YouTube JS challenges may fail"
fi

# Verify yt-dlp version
echo "[STARTUP] yt-dlp version: $(python -c 'import yt_dlp; print(yt_dlp.version.__version__)')"

echo "[STARTUP] Starting Gogu music bot..."

# Verify bgutil-ytdlp-pot-provider plugin is detected by yt-dlp
echo "[STARTUP] yt-dlp PO Token plugins:"
python -c "
import yt_dlp
ydl = yt_dlp.YoutubeDL({'quiet': True})
# Check if pot provider is loaded
try:
    from yt_dlp_plugins.extractor import getpot_bgutil
    print('  bgutil-ytdlp-pot-provider: LOADED')
except ImportError as e:
    print(f'  bgutil-ytdlp-pot-provider: NOT FOUND ({e})')
import importlib.metadata as _md
print('  bgutil plugin version:', _md.version('bgutil-ytdlp-pot-provider'))
print('  base_url: plugin default http://127.0.0.1:4416')
" 2>&1 || echo "[STARTUP] Plugin check failed"

# Quick verbose test to see if PO Token is being generated (always run for debug)
# DISABLED — probe consumes the fresh YouTube session and causes 429 for the bot
# echo "[STARTUP] Running verbose yt-dlp probe..."

# Verify PO Token server is responding. /ping is the endpoint the plugin itself
# probes and it returns the server version, so this doubles as a version check.
if command -v curl &> /dev/null; then
    POT_PING=$(curl -s --max-time 5 http://127.0.0.1:4416/ping 2>/dev/null || echo "failed")
    if [ "$POT_PING" = "failed" ] || [ -z "$POT_PING" ]; then
        echo "[STARTUP] WARNING: PO Token server /ping unreachable"
    else
        echo "[STARTUP] PO Token server /ping: $POT_PING"
    fi
fi

exec python bot.py
