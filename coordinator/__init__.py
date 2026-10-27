"""
LIFE Compute — Coordinator client package (C3)

Provides:
  CoordinatorClient   — signed GET /work, POST /results, result queue + backoff
  ShardManager        — download shards, verify SHA-256, LRU disk cache
  register_command    — CLI one-shot: on-chain register_miner + coordinator /register
"""

from .client import CoordinatorClient  # noqa: F401
from .shards import ShardManager        # noqa: F401
from .register import register_command  # noqa: F401
