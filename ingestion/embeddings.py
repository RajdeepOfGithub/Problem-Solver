"""
ingestion/embeddings.py
Vega — Phase 2: Core Intelligence Layer

Sends code chunks to Amazon Nova Multimodal Embeddings via Bedrock
(boto3 InvokeModel) and returns embedding vectors.

Design rules:
- Boto3 first: InvokeModel against amazon.nova-2-multimodal-embeddings-v1:0
- Batching: Bedrock accepts one embedding per call; we parallelize with a
  thread pool to keep indexing fast without exceeding service limits
- Rate limiting: exponential backoff on ThrottlingException
- Returns a list of EmbeddedChunk dicts — consumed directly by vector_store.py
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypedDict

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Model ID matches ENV.md: NOVA_EMBEDDING_MODEL_ID
DEFAULT_MODEL_ID = os.getenv("NOVA_EMBEDDING_MODEL_ID", "amazon.nova-2-multimodal-embeddings-v1:0")
DEFAULT_REGION   = os.getenv("AWS_BEDROCK_REGION", os.getenv("AWS_REGION", "us-east-1"))

# Bedrock embedding throughput limits — tune these for your account tier
MAX_WORKERS      = 5     # parallel InvokeModel calls
MAX_RETRIES      = 4
BASE_BACKOFF     = 1.0   # seconds, doubles on each retry

# Nova Multimodal Embeddings max input length (characters)
# Text longer than this is truncated — chunking in repo_loader keeps us well under
MAX_CONTENT_CHARS = 8000

# Nova 2 Multimodal Embeddings output dimension (1024 = best accuracy/storage balance)
EMBEDDING_DIMENSION = 1024


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class EmbeddedChunk(TypedDict):
    """A chunk annotated with its embedding vector."""
    file:       str
    start_line: int
    end_line:   int
    content:    str
    language:   str
    repo_url:   str
    branch:     str
    embedding:  list[float]   # Nova Multimodal Embeddings output vector


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def embed_chunks(
    chunks: list[dict],
    model_id: str | None = None,
    region: str | None = None,
    max_workers: int = MAX_WORKERS,
) -> list[EmbeddedChunk]:
    """
    Embed all *chunks* using Nova Multimodal Embeddings.

    Returns a list of EmbeddedChunk dicts (same as input but with
    `embedding` field added). Order is preserved.

    Args:
        chunks:      Output of repo_loader.load_repo() — list of chunk dicts.
        model_id:    Bedrock model ID. Defaults to NOVA_EMBEDDING_MODEL_ID env var.
        region:      AWS region. Defaults to AWS_BEDROCK_REGION env var.
        max_workers: Parallel embedding threads.

    Raises:
        RuntimeError — if Bedrock is unreachable or credentials are missing.
    """
    if not chunks:
        return []

    model  = model_id or DEFAULT_MODEL_ID
    reg    = region   or DEFAULT_REGION
    client = _get_bedrock_client(reg)

    logger.info(
        "Embedding %d chunks via %s in %s (workers=%d)",
        len(chunks), model, reg, max_workers,
    )

    results: list[EmbeddedChunk | None] = [None] * len(chunks)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {
            pool.submit(_embed_single, client, model, chunk): idx
            for idx, chunk in enumerate(chunks)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                embedded = future.result()
                results[idx] = embedded
            except Exception as exc:
                logger.error("Failed to embed chunk %d (%s L%s): %s",
                             idx, chunks[idx].get("file"), chunks[idx].get("start_line"), exc)
                raise

    # type: ignore — all slots filled by this point
    embedded_chunks: list[EmbeddedChunk] = results  # type: ignore[assignment]
    logger.info("Embedding complete — %d vectors generated", len(embedded_chunks))
    return embedded_chunks


def embed_query(
    query: str,
    model_id: str | None = None,
    region: str | None = None,
) -> list[float]:
    """
    Embed a single natural-language query string for similarity search.
    Used at retrieval time (not during indexing).

    Returns a flat list of floats (the embedding vector).
    """
    model  = model_id or DEFAULT_MODEL_ID
    reg    = region   or DEFAULT_REGION
    client = _get_bedrock_client(reg)

    logger.debug("Embedding query (%d chars) via %s", len(query), model)
    vector = _invoke_embedding(client, model, _truncate(query), purpose="GENERIC_RETRIEVAL")
    return vector


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_bedrock_client(region: str):
    """Return a boto3 bedrock-runtime client. Credentials come from the env."""
    try:
        return boto3.client(
            "bedrock-runtime",
            region_name=region,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to create Bedrock client: {exc}") from exc


def _embed_single(client, model_id: str, chunk: dict) -> EmbeddedChunk:
    """Embed one chunk and return it with the embedding attached."""
    text = _truncate(chunk["content"])
    vector = _invoke_embedding(client, model_id, text)
    return EmbeddedChunk(
        file=chunk["file"],
        start_line=chunk["start_line"],
        end_line=chunk["end_line"],
        content=chunk["content"],
        language=chunk["language"],
        repo_url=chunk["repo_url"],
        branch=chunk["branch"],
        embedding=vector,
    )


def _invoke_embedding(
    client,
    model_id: str,
    text: str,
    purpose: str = "GENERIC_INDEX",
) -> list[float]:
    """
    Call Bedrock InvokeModel for Nova 2 Multimodal Embeddings with retry/backoff.

    Nova 2 Multimodal Embeddings request body:
        {
            "taskType": "SINGLE_EMBEDDING",
            "singleEmbeddingParams": {
                "embeddingPurpose": "GENERIC_INDEX" | "GENERIC_RETRIEVAL",
                "embeddingDimension": 1024,
                "text": {"truncationMode": "END", "value": "<text>"}
            }
        }

    Response body:
        { "embedding": [float, ...] }

    Args:
        purpose: "GENERIC_INDEX" for indexing chunks, "GENERIC_RETRIEVAL" for queries.
    """
    body = json.dumps({
        "taskType": "SINGLE_EMBEDDING",
        "singleEmbeddingParams": {
            "embeddingPurpose": purpose,
            "embeddingDimension": EMBEDDING_DIMENSION,
            "text": {
                "truncationMode": "END",
                "value": text,
            },
        },
    })
    backoff = BASE_BACKOFF

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.invoke_model(
                modelId=model_id,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            response_body = json.loads(response["body"].read())
            # Nova 2 Multimodal Embeddings: {"embeddings": [{"embeddingType": "TEXT", "embedding": [...]}]}
            return response_body["embeddings"][0]["embedding"]

        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("ThrottlingException", "TooManyRequestsException"):
                if attempt == MAX_RETRIES:
                    raise RuntimeError(
                        f"Bedrock throttled after {MAX_RETRIES} retries: {exc}"
                    ) from exc
                sleep_time = backoff * (2 ** (attempt - 1))
                logger.warning(
                    "Bedrock throttle (attempt %d/%d) — sleeping %.1fs",
                    attempt, MAX_RETRIES, sleep_time,
                )
                time.sleep(sleep_time)
            elif code in ("ModelNotReadyException", "ServiceUnavailableException"):
                if attempt == MAX_RETRIES:
                    raise RuntimeError(f"Bedrock unavailable: {exc}") from exc
                time.sleep(backoff * attempt)
            else:
                raise RuntimeError(f"Bedrock error ({code}): {exc}") from exc

    raise RuntimeError("Embedding failed after all retries")  # should never reach here


def _truncate(text: str) -> str:
    """Truncate text to MAX_CONTENT_CHARS. Logs a warning if truncation occurs."""
    if len(text) <= MAX_CONTENT_CHARS:
        return text
    logger.warning(
        "Content truncated from %d to %d chars for embedding",
        len(text), MAX_CONTENT_CHARS,
    )
    return text[:MAX_CONTENT_CHARS]
