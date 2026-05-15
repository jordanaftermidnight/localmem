"""LOCALMEM embedder — local embedding model management."""

from __future__ import annotations

import logging

from qdrant_client.models import SparseVector

from .config import LocalmemConfig

logger = logging.getLogger(__name__)

DENSE_DIMS = {
    "all-MiniLM-L6-v2": 384,
    "bge-large-en-v1.5": 1024,
    "nomic-embed-text-v1.5": 768,
}


class Embedder:
    """Manages dense and sparse embedding models for LOCALMEM."""

    def __init__(self, config: LocalmemConfig):
        self.config = config
        self._dense_model = None
        self._sparse_model = None
        self._resolved_device: str = config.embedding.device

    @property
    def dense_dim(self) -> int:
        return DENSE_DIMS.get(self.config.embedding.model, 384)

    @property
    def resolved_device(self) -> str:
        return self._resolved_device

    @staticmethod
    def _detect_device() -> str:
        """Auto-detect the best available compute device."""
        try:
            import torch

            if hasattr(torch.backends, "mps"):
                try:
                    if torch.backends.mps.is_available():
                        return "mps"
                except (RuntimeError, AttributeError):
                    logger.warning("MPS detection failed, skipping")
            if torch.cuda.is_available():
                return "cuda"
        except ImportError:
            pass
        return "cpu"

    def load(self) -> None:
        """Load embedding models. Call once during initialization."""
        from sentence_transformers import SentenceTransformer

        device = self.config.embedding.device
        if device == "auto":
            device = self._detect_device()
        self._resolved_device = device

        self._dense_model = SentenceTransformer(
            self.config.embedding.model,
            device=device,
        )
        logger.info(
            f"Dense model loaded: {self.config.embedding.model} "
            f"({self.dense_dim}d, device={self._resolved_device})"
        )

        try:
            from fastembed import SparseTextEmbedding

            self._sparse_model = SparseTextEmbedding(
                model_name=self.config.embedding.sparse_model
            )
            logger.info(f"Sparse model loaded: {self.config.embedding.sparse_model}")
        except Exception:
            logger.warning("Sparse embedding model unavailable; dense-only search.")
            self._sparse_model = None

    def embed_dense(self, text: str) -> list[float]:
        """Generate dense embedding vector for text."""
        return self._dense_model.encode(text).tolist()

    def embed_sparse(self, text: str) -> SparseVector | None:
        """Generate sparse embedding vector for text, or None if unavailable."""
        if self._sparse_model is None:
            return None
        results = list(self._sparse_model.embed([text]))
        if not results:
            return None
        sparse = results[0]
        return SparseVector(
            indices=sparse.indices.tolist(),
            values=sparse.values.tolist(),
        )

    @property
    def has_sparse(self) -> bool:
        return self._sparse_model is not None
