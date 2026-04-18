FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libsodium-dev curl git unzip \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Deno (required by yt-dlp for YouTube JS challenges)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# PO Token provider (bgutil) — use latest stable
RUN git clone --single-branch \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/pot-provider \
    && cd /opt/pot-provider/server && npm ci && npx tsc

# Install the yt-dlp plugin + ensure latest yt-dlp with JS challenge support
RUN pip install --no-cache-dir bgutil-ytdlp-pot-provider \
    && pip install --no-cache-dir -U "yt-dlp[default]"

COPY . .
COPY start.sh .
RUN chmod +x start.sh

# Tell bgutil plugin where the PO Token server lives
ENV GETPOT_BGUTIL_BASE_URL=http://127.0.0.1:4416

EXPOSE 4416 8080

CMD ["./start.sh"]
