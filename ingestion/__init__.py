"""
ingestion/
Vega — Phase 2: Core Intelligence Layer
"""
from ingestion.repo_loader import load_repo, chunk_file_content
from ingestion.embeddings import embed_chunks, embed_query
from ingestion.vector_store import VectorStore, get_default_store

__all__ = [
    "load_repo", "chunk_file_content",
    "embed_chunks", "embed_query",
    "VectorStore", "get_default_store",
]
