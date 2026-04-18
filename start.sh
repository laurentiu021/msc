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
import os
print(f'  GETPOT_BGUTIL_BASE_URL={os.getenv(\"GETPOT_BGUTIL_BASE_URL\", \"NOT SET\")}')
" 2>&1 || echo "[STARTUP] Plugin check failed"

# Quick verbose test to see if PO Token is being generated (always run for debug)
echo "[STARTUP] Running verbose yt-dlp probe..."
python -c "
import yt_dlp, json, sys
opts = {
    'quiet': False, 'verbose': True, 'skip_download': True,
    'format': 'best', 'socket_timeout': 10,
    'extractor_args': {'youtube': 'player_client=mweb'},
    'ignore_no_formats_error': True,
}
try:
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info('https://www.youtube.com/watch?v=dQw4w9WgXcQ', download=False)
        fmts = info.get('formats', [])
        real = sum(1 for f in fmts if f.get('acodec','none') != 'none')
        print(f'[STARTUP] Probe result: {len(fmts)} formats ({real} real audio)')
except Exception as e:
    print(f'[STARTUP] Probe failed: {e}')
" 2>&1 | grep -iE '(pot|token|plugin|formats|real|probe|error|warning|sabr|challenge|deno|jsc|getpot|bgutil)' | head -30

# Verify PO Token server is responding
if command -v curl &> /dev/null; then
    POT_TEST=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:4416/token 2>/dev/null || echo "failed")
    if [ "$POT_TEST" = "failed" ]; then
        echo "[STARTUP] WARNING: PO Token server health check failed"
    else
        echo "[STARTUP] PO Token server reachable (HTTP $POT_TEST)"
    fi
fi

exec python bot.py
