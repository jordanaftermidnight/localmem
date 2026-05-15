"""Shared pytest configuration and fixtures."""

import sys
from pathlib import Path

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class FakeEmbedder:
    """Hash-based embedder for tests — no model downloads required."""

    dense_dim = 8
    _resolved_device = "cpu"

    @property
    def resolved_device(self) -> str:
        return self._resolved_device

    def embed_dense(self, text: str) -> list[float]:
        h = hash(text) & 0xFFFFFFFF
        return [(h >> i & 0xFF) / 255.0 for i in range(0, 64, 8)]

    def embed_sparse(self, text: str):
        return None

    @property
    def has_sparse(self) -> bool:
        return False
