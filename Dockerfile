FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libsodium-dev curl git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# PO Token provider (bgutil)
RUN git clone --single-branch --branch 1.3.1 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/pot-provider \
    && cd /opt/pot-provider/server && npm ci && npx tsc

RUN pip install --no-cache-dir bgutil-ytdlp-pot-provider

COPY . .
COPY start.sh .
RUN chmod +x start.sh

EXPOSE 4416 8080

CMD ["./start.sh"]
