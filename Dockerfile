# ─── LIFE Compute Miner ────────────────────────────────────────────────────────
# Base: ubuntu:22.04 via the official NVIDIA CUDA runtime layer
# Your GPU could help cure cancer. Earn $LIFE tokens.
# ──────────────────────────────────────────────────────────────────────────────
# nvidia/cuda:12.2.0-runtime-ubuntu22.04 is ubuntu:22.04 + CUDA runtime.
# Using it instead of plain ubuntu:22.04 + manual CUDA install saves ~2 GB
# and ensures driver compatibility. Same base OS, GPU-ready out of the box.
FROM nvidia/cuda:12.2.0-runtime-ubuntu22.04
# ubuntu:22.04 base confirmed — verify with: docker run --rm <image> cat /etc/lsb-release

LABEL org.opencontainers.image.title="LIFE Compute Miner"
LABEL org.opencontainers.image.description="Decentralized cancer drug discovery miner. Your GPU could help cure cancer. Earn \$LIFE tokens."
LABEL org.opencontainers.image.source="https://github.com/life-compute/miner"
LABEL org.opencontainers.image.licenses="MIT"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# ─── System dependencies ────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 \
        python3.10-dev \
        python3.10-distutils \
        python3-pip \
        python3-venv \
        git \
        curl \
        wget \
        ca-certificates \
        libssl-dev \
        libffi-dev \
        build-essential \
        # CUDA dev headers for GPU-accelerated libraries
        libcuda1 \
        libcufft-11 \
    && rm -rf /var/lib/apt/lists/*

# ─── Set python3.10 as default ─────────────────────────────────────────────
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1 \
    && update-alternatives --install /usr/bin/python  python  /usr/bin/python3.10 1

# ─── Upgrade pip ───────────────────────────────────────────────────────────
RUN python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel

# ─── Python dependencies ───────────────────────────────────────────────────
# Install Boltz2 (molecular structure prediction)
RUN python3 -m pip install --no-cache-dir \
        boltz==0.4.2 \
        solders==0.21.0 \
        anchorpy==0.20.1 \
        httpx==0.27.0 \
        aiohttp==3.9.5 \
        base58==2.1.1 \
        cryptography==42.0.8 \
        pydantic==2.7.4 \
        rich==13.7.1 \
        numpy==1.26.4

# ─── App directory ─────────────────────────────────────────────────────────
WORKDIR /app

# Copy miner daemon
COPY miner_daemon.py /app/miner_daemon.py

# Copy dashboard build (built separately via `npm run build`)
COPY dashboard/dist /app/dashboard/dist

# ─── Config & data directories ─────────────────────────────────────────────
RUN mkdir -p /root/.life-compute

# ─── Health check ──────────────────────────────────────────────────────────
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import json,os; s=json.load(open('/root/.life-compute/stats.json')); exit(0 if s.get('alive') else 1)" || exit 1

# ─── Expose dashboard port ─────────────────────────────────────────────────
EXPOSE 8765

ENTRYPOINT ["python3", "miner_daemon.py"]
