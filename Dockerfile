FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libsodium-dev curl git unzip \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Deno (required by yt-dlp for YouTube JS challenges)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# PO Token provider (bgutil) server — pinned to the same version as the pip plugin.
# Server >=1.3.2 needs Node >=22 (see server/package.json engines).
ARG BGUTIL_VERSION=2.0.0
RUN git clone --single-branch --depth 1 --branch ${BGUTIL_VERSION} \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/pot-provider \
    && cd /opt/pot-provider/server && npm ci && npx tsc

COPY . .
COPY start.sh .
RUN chmod +x start.sh

# No base_url env var needed: since plugin 2.0.0 the only knobs are the
# 'youtubepot-bgutilhttp:base_url' extractor arg and the default, which is
# already http://127.0.0.1:4416 — exactly where start.sh runs the server.

EXPOSE 4416 8080

CMD ["./start.sh"]
