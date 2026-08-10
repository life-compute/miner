FROM ubuntu:22.04
LABEL maintainer="life-compute" \
      description="LIFE Compute Miner — decentralized cancer drug discovery"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 python3.10-dev python3-pip python3.10-venv \
        curl wget git build-essential \
        libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Python deps
RUN pip3 install --no-cache-dir \
    boltz==0.4.2 \
    anchorpy==0.20.1 \
    solders==0.21.0 \
    solana==0.34.0 \
    requests==2.32.0 \
    aiohttp==3.9.5

WORKDIR /app
COPY miner_daemon.py .
COPY stats.json.template stats.json

RUN useradd -m miner
USER miner

EXPOSE 3000
ENTRYPOINT ["python3", "miner_daemon.py"]
