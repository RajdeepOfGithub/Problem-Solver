"""
ingestion/vector_store.py
Vega — Phase 2: Core Intelligence Layer

FAISS-backed vector store for Vega's knowledge base.
Stores embedded chunks, persists to disk, and supports fast top-k
similarity search used by all downstream agents.

Design rules:
- FAISS (faiss-cpu) is the default; OpenSearch is an optional swap-in
  controlled by VECTOR_STORE_TYPE env var
- Metadata (file, line, content, language) is stored separately from
  the FAISS index (FAISS only holds vectors + integer IDs)
- Thread-safe: a single lock guards all writes
- Index is persisted to FAISS_INDEX_PATH after every add() call
- Query returns chunk dicts with an extra `score` field (cosine similarity,
  higher = more relevant) — ready to inject directly into agent context payloads
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import TypedDict

import faiss
import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_INDEX_PATH = os.getenv("FAISS_INDEX_PATH", "./data/faiss_index")

# FAISS index filenames
INDEX_FILE    = "index.faiss"
METADATA_FILE = "metadata.json"

# Top-k default used by agents — can be overridden per query
DEFAULT_TOP_K = 8


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class RetrievedChunk(TypedDict):
    """A chunk returned by a similarity search, with score attached."""
    file:       str
    start_line: int
    end_line:   int
    content:    str
    language:   str
    repo_url:   str
    branch:     str
    score:      float   # cosine similarity in [0, 1], higher = more similar


# ---------------------------------------------------------------------------
# VectorStore
# ---------------------------------------------------------------------------

class VectorStore:
    """
    FAISS-backed vector store for Vega.

    Usage:
        store = VectorStore()                    # load existing index or create new
        store.add(embedded_chunks)               # add EmbeddedChunk dicts
        results = store.query(query_vector, k=8) # returns list[RetrievedChunk]
        store.save()                             # persist to disk (also called on add())
    """

    def __init__(self, index_path: str | None = None):
        self._path    = Path(index_path or DEFAULT_INDEX_PATH)
        self._lock    = threading.Lock()
        self._index: faiss.Index | None = None
        self._metadata: list[dict]      = []   # parallel list indexed by FAISS internal ID
        self._dimension: int | None     = None

        self._path.mkdir(parents=True, exist_ok=True)
        self._load_if_exists()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, embedded_chunks: list[dict]) -> int:
        """
        Add a list of EmbeddedChunk dicts to the store.

        Returns the total number of vectors now in the index.
        Persists to disk after every call.

        Args:
            embedded_chunks: Output of embeddings.embed_chunks() — must have
                             an `embedding` field plus standard chunk fields.
        """
        if not embedded_chunks:
            return self.size

        vectors  = np.array([c["embedding"] for c in embedded_chunks], dtype=np.float32)
        dim      = vectors.shape[1]

        with self._lock:
            if self._index is None:
                self._dimension = dim
                self._index = self._build_index(dim)
                logger.info("Created new FAISS index (dim=%d)", dim)
            elif dim != self._dimension:
                raise ValueError(
                    f"Embedding dimension mismatch: index has {self._dimension}, "
                    f"new vectors have {dim}"
                )

            # Normalize for cosine similarity (FAISS IndexFlatIP on normalized = cosine)
            faiss.normalize_L2(vectors)
            self._index.add(vectors)

            # Store metadata in parallel list
            for chunk in embedded_chunks:
                meta: dict = {
                    "file":       chunk["file"],
                    "start_line": chunk["start_line"],
                    "end_line":   chunk["end_line"],
                    "content":    chunk["content"],
                    "language":   chunk["language"],
                    "repo_url":   chunk["repo_url"],
                    "branch":     chunk["branch"],
                }
                # Preserve index_id if set by index_chunks() for per-job retrieval
                if "index_id" in chunk:
                    meta["index_id"] = chunk["index_id"]
                self._metadata.append(meta)

            total = self._index.ntotal
            logger.info("Added %d vectors — index total: %d", len(embedded_chunks), total)

        self.save()
        return total

    def query(
        self,
        query_vector: list[float],
        k: int = DEFAULT_TOP_K,
        filter_language: str | None = None,
        filter_file_prefix: str | None = None,
    ) -> list[RetrievedChunk]:
        """
        Return the top-k most similar chunks for *query_vector*.

        Args:
            query_vector:      Embedding vector from embeddings.embed_query().
            k:                 Number of results to return.
            filter_language:   If set, only return chunks with this language.
            filter_file_prefix: If set, only return chunks whose file path starts
                                with this prefix (e.g. "src/auth/").

        Returns:
            List of RetrievedChunk dicts sorted by score descending.
        """
        if self._index is None or self._index.ntotal == 0:
            logger.warning("Query called on empty index — returning []")
            return []

        vec = np.array([query_vector], dtype=np.float32)
        faiss.normalize_L2(vec)

        # Fetch extra candidates if we're going to filter
        fetch_k = k * 4 if (filter_language or filter_file_prefix) else k

        with self._lock:
            distances, indices = self._index.search(vec, min(fetch_k, self._index.ntotal))

        results: list[RetrievedChunk] = []
        for score, idx in zip(distances[0], indices[0]):
            if idx < 0 or idx >= len(self._metadata):
                continue  # FAISS pads with -1 if fewer results than k

            meta = self._metadata[idx]

            if filter_language and meta["language"] != filter_language:
                continue
            if filter_file_prefix and not meta["file"].startswith(filter_file_prefix):
                continue

            results.append(RetrievedChunk(
                file=meta["file"],
                start_line=meta["start_line"],
                end_line=meta["end_line"],
                content=meta["content"],
                language=meta["language"],
                repo_url=meta["repo_url"],
                branch=meta["branch"],
                score=float(score),
            ))

            if len(results) >= k:
                break

        return results

    def index_chunks(self, embedded_chunks: list[dict], index_id: str | None = None) -> int:
        """
        Alias for add() that optionally tags each chunk with index_id (job_id).
        Allows per-job retrieval via get_all_chunks(index_id).

        Args:
            embedded_chunks: Output of embeddings.embed_chunks().
            index_id:        Optional job/repo ID to tag chunks with.

        Returns:
            Total number of vectors in the index after addition.
        """
        if index_id:
            for chunk in embedded_chunks:
                chunk.setdefault("index_id", index_id)
        return self.add(embedded_chunks)

    def get_all_chunks(self, index_id: str | None = None) -> list[dict]:
        """
        Return all stored metadata chunks, optionally filtered by index_id.

        Args:
            index_id: If provided, only return chunks tagged with this ID.

        Returns:
            List of chunk dicts (without embeddings — those live in FAISS only).
        """
        with self._lock:
            if index_id is not None:
                return [m for m in self._metadata if m.get("index_id") == index_id]
            return list(self._metadata)

    def save(self) -> None:
        """Persist FAISS index and metadata to disk."""
        with self._lock:
            if self._index is None:
                return
            faiss.write_index(self._index, str(self._path / INDEX_FILE))
            with open(self._path / METADATA_FILE, "w", encoding="utf-8") as f:
                json.dump(self._metadata, f, ensure_ascii=False)
        logger.debug("VectorStore saved to %s (%d vectors)", self._path, self.size)

    def reset(self) -> None:
        """Wipe the entire index. Used when re-indexing a repo from scratch."""
        with self._lock:
            self._index    = None
            self._metadata = []
            self._dimension = None
            index_file = self._path / INDEX_FILE
            meta_file  = self._path / METADATA_FILE
            if index_file.exists():
                index_file.unlink()
            if meta_file.exists():
                meta_file.unlink()
        logger.info("VectorStore reset at %s", self._path)

    @property
    def size(self) -> int:
        """Number of vectors currently in the index."""
        if self._index is None:
            return 0
        return int(self._index.ntotal)

    @property
    def dimension(self) -> int | None:
        """Embedding dimension of the index, or None if empty."""
        return self._dimension

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_if_exists(self) -> None:
        index_file = self._path / INDEX_FILE
        meta_file  = self._path / METADATA_FILE

        if index_file.exists() and meta_file.exists():
            try:
                self._index = faiss.read_index(str(index_file))
                with open(meta_file, "r", encoding="utf-8") as f:
                    self._metadata = json.load(f)
                self._dimension = self._index.d
                logger.info(
                    "Loaded existing FAISS index: %d vectors, dim=%d, path=%s",
                    self._index.ntotal, self._dimension, self._path,
                )
            except Exception as exc:
                logger.warning("Failed to load existing index (%s) — starting fresh", exc)
                self._index    = None
                self._metadata = []
        else:
            logger.info("No existing FAISS index at %s — will create on first add()", self._path)

    @staticmethod
    def _build_index(dim: int) -> faiss.Index:
        """
        Build a FAISS IndexFlatIP (inner product on normalized vectors = cosine similarity).
        For production with >1M vectors, swap to IndexIVFFlat or IndexHNSWFlat here.
        """
        return faiss.IndexFlatIP(dim)


# ---------------------------------------------------------------------------
# Convenience singleton — used by the ingestion pipeline and agents
# ---------------------------------------------------------------------------

_default_store: VectorStore | None = None


def get_default_store() -> VectorStore:
    """Return the process-wide default VectorStore, creating it on first call."""
    global _default_store
    if _default_store is None:
        _default_store = VectorStore()
    return _default_store
