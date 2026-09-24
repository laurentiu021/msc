# Runtime-urile JS, pinuite ca yt-dlp si bgutil (vezi requirements.txt). Deno
# rezolva provocarile JS ale YouTube-ului, Node ruleaza serverul de PO Token: cand
# erau instalate prin scripturi descarcate la build (`deno.land/install.sh | sh`,
# `setup_24.x | bash`), orice rebuild — chiar fara nicio schimbare in repo — le
# putea schimba pe amandoua, fara nicio urma. Declarate inainte de primul FROM ca
# sa poata fi folosite in liniile FROM; CI-ul verifica ce ajunge in imagine.
ARG NODE_VERSION=24.21.0
ARG DENO_VERSION=2.9.6

# --- Etapa 1: serverul de PO Token -------------------------------------------
# Separata ca sa nu rămâna in imaginea finala nici git, nici toolchain-ul de
# TypeScript, nici dev-dependencies din node_modules. Din tot ce se construiește
# aici pleaca mai departe DOAR `build/`, dependentele de producție si executabilul
# `node` — acelasi Node care construieste serverul il si ruleaza.
FROM node:${NODE_VERSION}-slim AS pot-builder

# PO Token provider (bgutil) — pinuit la aceeasi versiune ca pluginul pip din
# requirements.txt. 2.0.0 patcheaza GHSA-qpv9-8xfj-xx9m (RCE prin bind pe 0.0.0.0)
# si aduce mintarea WebPO din 1.3.2, care atenueaza 403-urile de la GVS.
# Serverul >=1.3.2 cere Node >=22 (vezi engines din server/package.json).
ARG BGUTIL_VERSION=2.0.0

# pipefail: un pas eșuat dintr-un pipe trebuie sa opreasca build-ul, nu sa fie
# acoperit de codul de ieșire al ultimului pas.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# `ca-certificates` NU e in node:24-slim, iar fara el `git clone` prin HTTPS cade
# cu "server certificate verification failed. CAfile: none". Imaginea python-slim
# il avea deja, de asta nu se vedea inainte de separarea in doua etape.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --single-branch --depth 1 --branch ${BGUTIL_VERSION} \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/pot-provider \
    && cd /opt/pot-provider/server \
    && npm ci \
    && npx tsc \
    # Dev-dependencies au existat doar pentru `tsc`. Scoase AICI, ca etapa finala
    # sa copieze un node_modules deja curat.
    && npm prune --omit=dev \
    && rm -rf /opt/pot-provider/.git


# --- Deno --------------------------------------------------------------------
# Imaginea oficiala `bin` contine doar executabilul, exact pentru COPY --from.
FROM denoland/deno:bin-${DENO_VERSION} AS deno


# --- Etapa 2: imaginea care ruleaza ------------------------------------------
FROM python:3.12-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# `curl` rămâne: start.sh il folosește ca sa aȘtepte pe starea REALA a serverului
# de PO Token, nu pe un `sleep` fix. `git` nu mai are ce sa caute aici — clonarea
# s-a intamplat in etapa de build.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libsodium-dev curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Node doar ca executabil: la runtime nu se foloseste npm, doar `node`, pentru
# serverul de PO Token si ca runtime JS de rezerva pentru yt-dlp.
COPY --from=pot-builder /usr/local/bin/node /usr/local/bin/node

# Deno pentru challenge-urile JS ale YouTube-ului. Node e activat ca REZERVA in
# `config.JS_RUNTIMES`: yt-dlp porneste implicit doar cu deno, deci fara asta o
# instalare de deno care se rupe ar face sa dispara tacit formatele opus.
COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=pot-builder /opt/pot-provider/server/build \
                        /opt/pot-provider/server/build
COPY --from=pot-builder /opt/pot-provider/server/node_modules \
                        /opt/pot-provider/server/node_modules
COPY --from=pot-builder /opt/pot-provider/server/package.json \
                        /opt/pot-provider/server/package.json

COPY . .
RUN chmod +x start.sh

# No base_url env var needed: since plugin 2.0.0 the only knobs are the
# 'youtubepot-bgutilhttp:base_url' extractor arg and the default, which is
# already http://127.0.0.1:4416 — exactly where start.sh runs the server.

EXPOSE 4416 8080

CMD ["./start.sh"]
