# --- Etapa 1: serverul de PO Token -------------------------------------------
# Separata ca sa nu rămâna in imaginea finala nici git, nici toolchain-ul de
# TypeScript, nici dev-dependencies din node_modules. Din tot ce se construiește
# aici pleaca mai departe DOAR `build/` si dependentele de producție.
FROM node:24-slim AS pot-builder

# PO Token provider (bgutil) — pinuit la aceeasi versiune ca pluginul pip din
# requirements.txt. 2.0.0 patcheaza GHSA-qpv9-8xfj-xx9m (RCE prin bind pe 0.0.0.0)
# si aduce mintarea WebPO din 1.3.2, care atenueaza 403-urile de la GVS.
# Serverul >=1.3.2 cere Node >=22 (vezi engines din server/package.json).
ARG BGUTIL_VERSION=2.0.0

# pipefail: fara el, un curl eșuat trimite pagina de eroare in bash, care iese
# cu 0, si build-ul continua mai departe cu un repo de Node lipsa.
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


# --- Etapa 2: imaginea care ruleaza ------------------------------------------
FROM python:3.12-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# `curl` rămâne: start.sh il folosește ca sa aȘtepte pe starea REALA a serverului
# de PO Token, nu pe un `sleep` fix. `git` si `unzip` nu mai au ce sa caute aici —
# clonarea s-a intamplat in etapa de build.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libsodium-dev curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Deno pentru challenge-urile JS ale YouTube-ului. Node e activat ca REZERVA in
# `config.JS_RUNTIMES`: yt-dlp porneste implicit doar cu deno, deci fara asta o
# instalare de deno care se rupe ar face sa dispara tacit formatele opus.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y

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
