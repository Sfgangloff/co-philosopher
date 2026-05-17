"""Offline text embeddings via fastembed (ONNX runtime, no torch).

The model is downloaded once to the HuggingFace cache, then runs fully
offline. Vectors are L2-normalized so a cosine store ranks them correctly.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Protocol

import numpy as np

from cophilo.config import Config


class EmbedderLike(Protocol):
    name: str
    dim: int

    def encode(self, texts: list[str]) -> np.ndarray: ...


class FastEmbedder:
    def __init__(self, model_name: str) -> None:
        self.name = model_name
        self._model = None
        self._dim: int | None = None

    def _ensure(self):
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self.name)
        return self._model

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = int(self.encode(["probe"]).shape[1])
        return self._dim

    def encode(self, texts: list[str]) -> np.ndarray:
        model = self._ensure()
        vecs = np.asarray(list(model.embed(list(texts))), dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        out = vecs / norms
        self._dim = int(out.shape[1])
        return out


@lru_cache(maxsize=4)
def get_embedder(model_name: str) -> FastEmbedder:
    return FastEmbedder(model_name)


def default_embedder(cfg: Config) -> FastEmbedder:
    return get_embedder(cfg.memory_embedding_model)
