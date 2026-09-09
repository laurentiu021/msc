#!/bin/bash
set -euo pipefail

POT_PORT="${POT_PORT:-4416}"
POT_URL="http://127.0.0.1:${POT_PORT}"

# Serverul de PO Token, supravegheat. Inainte era pornit o singura data si
# nimeni nu se mai uita la el: daca procesul node murea, botul rămânea "sanatos"
# si fiecare redare eseua cu 403, la infinit, fara nimic in loguri.
supervise_pot() {
    while true; do
        # `|| rc=$?`, nu `|| true`: cu `|| true` variabila $? de pe linia
        # urmatoare era codul lui `true`, adica mereu 0, deci un OOM kill si o
        # ieșire curata arata identic si singurul diagnostic al supervizorului
        # era mereu greșit.
        rc=0
        node /opt/pot-provider/server/build/main.js --port "$POT_PORT" || rc=$?
        echo "[SUPERVISOR] PO Token server a ieșit (cod ${rc}). Repornesc in 5s."
        sleep 5
    done
}

echo "[STARTUP] Pornesc PO Token server pe portul ${POT_PORT}..."
supervise_pot &
POT_SUPERVISOR_PID=$!

# Asteptare pe starea REALA, nu `sleep 3`. Serverul are nevoie de ~5s la boot,
# deci verificarea fixa raporta fals "unreachable" la fiecare pornire mai lenta.
POT_READY=0
for _ in $(seq 1 60); do
    if curl -fsS --max-time 2 "${POT_URL}/ping" >/dev/null 2>&1; then
        POT_READY=1
        break
    fi
    sleep 0.5
done

# POT_READY=1/0 e un token STABIL, de caut in loguri. Mesajul din errori
# (music/errors.py) trimitea utilizatorul sa caute "[STARTUP] PO Token server
# running", un text care nu exista nicaieri in proiect.
echo "[STARTUP] POT_READY=${POT_READY}"
if [ "$POT_READY" = "1" ]; then
    echo "[STARTUP] PO Token server gata: $(curl -fsS --max-time 2 "${POT_URL}/ping")"
else
    echo "[STARTUP] AVERTISMENT: PO Token server nu a raspuns in 30s."
    echo "[STARTUP] Botul porneste oricum, dar descarcarile vor primi 403."
fi

if command -v deno >/dev/null 2>&1; then
    echo "[STARTUP] Deno disponibil: $(deno --version | head -1)"
else
    echo "[STARTUP] AVERTISMENT: Deno lipseste — provocarile JS de la YouTube vor eșua"
fi

echo "[STARTUP] yt-dlp: $(python -c 'import yt_dlp; print(yt_dlp.version.__version__)')"

# Pluginul de PO Token e detectat de yt-dlp?
python - <<'PY' || echo "[STARTUP] Verificarea pluginului a eșuat"
import importlib.metadata as md
try:
    from yt_dlp_plugins.extractor import getpot_bgutil  # noqa: F401
    print('[STARTUP] bgutil-ytdlp-pot-provider: INCARCAT', md.version('bgutil-ytdlp-pot-provider'))
except ImportError as e:
    print(f'[STARTUP] bgutil-ytdlp-pot-provider: LIPSESTE ({e})')
PY

echo "[STARTUP] Pornesc botul Gogu..."

# exec: python devine PID 1 si primeste direct SIGTERM de la Railway, iar bot.py
# isi inchide conexiunea de voce curat. Supervizorul rămâne copil si e oprit de
# runtime odata cu containerul.
exec python bot.py
