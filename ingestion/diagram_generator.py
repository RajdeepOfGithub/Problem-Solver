"""
ingestion/diagram_generator.py
Vega — Phase 2: Core Intelligence Layer

Generates a Mermaid flowchart diagram of a repo's structure immediately
after the ingestion pipeline completes. Called by the API server after
indexing finishes to populate GET /repo/diagram/{job_id}.

Design rules:
- Nova 2 Lite (via Bedrock boto3) generates the Mermaid text
- Server-side validation required before returning — fallback to plain text
  file tree on failure; never return empty
- Diagram level: file-level for repos <30 files, folder-level for 30-100
- Node IDs in the returned mermaid use sanitized alphanumeric identifiers
  (slashes/dots → underscores). The `node_ids` list maps these back to the
  original file/folder paths so the Codebase Explorer Agent and frontend
  can resolve highlighted_nodes by path.
- No imports from agents, voice, actions, or api — ingestion is self-contained
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MODEL_ID  = os.getenv("NOVA_LITE_MODEL_ID", "amazon.nova-lite-v1:0")
_REGION    = os.getenv("AWS_BEDROCK_REGION", os.getenv("AWS_REGION", "us-east-1"))

FILE_LEVEL_THRESHOLD   = 30   # repos with fewer files → file-level diagram
MAX_PROMPT_FILE_LINES  = 150  # cap the file tree sent to the model


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_diagram(
    file_tree: list[str],
    import_graph: dict[str, list[str]],
    file_count: int,
) -> dict:
    """
    Generate a Mermaid diagram for the indexed repository.

    Args:
        file_tree:    All repo-relative file paths that were indexed.
        import_graph: Output of repo_loader.build_import_graph() —
                      { file_path: [imported_file_paths] }.
        file_count:   Total number of indexable files (used to pick diagram level).

    Returns a dict with:
        {
            "mermaid":       str  — Mermaid flowchart string (or plain-text
                                   file tree if fallback_used is True),
            "diagram_level": str  — "file" | "folder",
            "node_ids":      list — original file/folder paths corresponding
                                   to each Mermaid node,
            "fallback_used": bool — True if Nova Lite or validation failed,
            "file_count":    int  — echo of input file_count,
        }
    """
    level = "file" if file_count < FILE_LEVEL_THRESHOLD else "folder"

    nodes: list[str]            # original paths used as logical node keys
    display_tree: list[str]     # what we pass to the model

    if level == "folder":
        nodes = _extract_folders(file_tree)
        display_tree = sorted(nodes)
    else:
        nodes = list(file_tree)
        display_tree = list(file_tree)

    logger.info(
        "Generating %s-level diagram for %d files / %d nodes",
        level, file_count, len(nodes),
    )

    prompt = _build_prompt(display_tree, import_graph, level)

    try:
        raw_mermaid = _call_nova_lite(prompt)
        raw_mermaid = _strip_markdown_fences(raw_mermaid)

        if _validate_mermaid(raw_mermaid):
            extracted_ids = _extract_node_ids(raw_mermaid)
            return {
                "mermaid":       raw_mermaid,
                "diagram_level": level,
                "node_ids":      extracted_ids if extracted_ids else nodes,
                "fallback_used": False,
                "file_count":    file_count,
            }

        logger.warning("Mermaid validation failed — using plain-text fallback")

    except Exception as exc:
        logger.error("Nova Lite diagram call failed: %s — using plain-text fallback", exc)

    fallback = _plain_text_file_tree(display_tree, level)
    return {
        "mermaid":       fallback,
        "diagram_level": level,
        "node_ids":      [],
        "fallback_used": True,
        "file_count":    file_count,
    }


def sanitize_path_to_node_id(path: str) -> str:
    """
    Convert a file/folder path to a valid Mermaid node ID.
    Replaces all non-alphanumeric characters with underscores.
    Prefixes with 'n' if the result starts with a digit.

    The frontend applies this same function to entries in highlighted_nodes
    to resolve them to the correct Mermaid node ID for highlighting.
    """
    safe = re.sub(r"[^A-Za-z0-9]", "_", path)
    safe = safe.strip("_")
    if safe and safe[0].isdigit():
        safe = "n" + safe
    return safe or "node"


# ---------------------------------------------------------------------------
# Internal helpers — prompt construction
# ---------------------------------------------------------------------------

def _build_prompt(
    paths: list[str],
    import_graph: dict[str, list[str]],
    level: str,
) -> str:
    """Build the Nova Lite prompt for Mermaid diagram generation."""
    # Cap the path list sent to the model
    truncated = paths[:MAX_PROMPT_FILE_LINES]
    tree_text = "\n".join(f"  - {p}" for p in truncated)
    if len(paths) > MAX_PROMPT_FILE_LINES:
        tree_text += f"\n  ... ({len(paths) - MAX_PROMPT_FILE_LINES} more)"

    # Build import relationship text — only include edges relevant to paths
    path_set = set(paths)
    edges: list[str] = []
    for src, targets in import_graph.items():
        if src not in path_set:
            continue
        for tgt in targets:
            if tgt in path_set:
                edges.append(f"  {src} --> {tgt}")
    edges_text = "\n".join(edges[:80]) if edges else "  (no import relationships found)"

    level_instruction = (
        "Each node represents a single FILE." if level == "file"
        else "Each node represents a FOLDER (group files by their top-level directory)."
    )

    return f"""You are generating a Mermaid flowchart diagram for a software repository.

{level_instruction}

REPOSITORY FILES:
{tree_text}

IMPORT/DEPENDENCY RELATIONSHIPS:
{edges_text}

INSTRUCTIONS:
1. Generate a valid Mermaid `flowchart TD` diagram.
2. Use the file or folder path as the node LABEL inside square brackets.
3. Use a sanitized node ID (alphanumeric and underscores only) derived from the path.
   Replace slashes and dots with underscores. Example: api/gateway.py → node ID: api_gateway_py, label: [api/gateway.py]
4. Add edges (-->) for each import/dependency relationship listed above.
5. Nodes with no relationships should still appear as standalone nodes.
6. Do NOT include markdown code fences (no ```). Output ONLY the Mermaid diagram text.
7. Start with exactly: flowchart TD

OUTPUT (Mermaid diagram only, starting with "flowchart TD"):"""


# ---------------------------------------------------------------------------
# Internal helpers — Nova Lite call
# ---------------------------------------------------------------------------

def _call_nova_lite(prompt: str) -> str:
    """
    Call Nova 2 Lite via Bedrock InvokeModel and return the text response.

    Raises RuntimeError if the call fails after a single attempt.
    (Diagram generation is non-critical; callers fall back to plain text.)
    """
    client = boto3.client(
        "bedrock-runtime",
        region_name=_REGION,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    )

    body = json.dumps({
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": prompt}]}
        ],
        "inferenceConfig": {
            "maxTokens": 2048,
            "temperature": 0.1,   # low temperature for deterministic output
        },
    })

    try:
        response = client.invoke_model(
            modelId=_MODEL_ID,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        response_body = json.loads(response["body"].read())

        # Nova Lite response structure: output.message.content[0].text
        return (
            response_body
            .get("output", {})
            .get("message", {})
            .get("content", [{}])[0]
            .get("text", "")
            .strip()
        )

    except ClientError as exc:
        raise RuntimeError(
            f"Bedrock InvokeModel failed ({exc.response['Error']['Code']}): {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Internal helpers — Mermaid validation & extraction
# ---------------------------------------------------------------------------

def _strip_markdown_fences(text: str) -> str:
    """Remove ```mermaid ... ``` fences if the model included them."""
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```\s*$", "", text)
    return text.strip()


def _validate_mermaid(mermaid: str) -> bool:
    """
    Server-side Mermaid validation.

    Checks:
    1. Starts with 'flowchart' (case-insensitive)
    2. No unclosed square brackets [ or round brackets (
    """
    if not mermaid:
        return False

    first_line = mermaid.strip().split("\n")[0].strip().lower()
    if not first_line.startswith("flowchart"):
        logger.debug("Mermaid validation failed: does not start with 'flowchart'")
        return False

    # Check balanced brackets — count opens vs closes
    if mermaid.count("[") != mermaid.count("]"):
        logger.debug("Mermaid validation failed: unbalanced square brackets")
        return False

    if mermaid.count("(") != mermaid.count(")"):
        logger.debug("Mermaid validation failed: unbalanced round brackets")
        return False

    return True


def _extract_node_ids(mermaid: str) -> list[str]:
    """
    Extract original file/folder paths from a generated Mermaid diagram.

    Looks for node declarations in the form:
        node_id[actual/path.py]
        node_id(actual/path.py)
        node_id{actual/path.py}

    Returns a deduplicated list of the label values (the actual paths),
    preserving first-occurrence order.
    """
    # Match: word_chars[label], word_chars(label), word_chars{label}
    pattern = re.compile(r'\b\w+[\[\({]([^\]\){}]+)[\]\){}]')
    seen: set[str] = set()
    ids: list[str] = []

    for match in pattern.finditer(mermaid):
        label = match.group(1).strip()
        # Only include labels that look like file/folder paths
        if label and ("/" in label or "." in label or label.endswith("/")) and label not in seen:
            seen.add(label)
            ids.append(label)

    return ids


# ---------------------------------------------------------------------------
# Internal helpers — folder extraction and fallback
# ---------------------------------------------------------------------------

def _extract_folders(file_tree: list[str]) -> list[str]:
    """
    Derive the unique top-level folder paths from a list of file paths.
    Files in the root (no parent directory) are represented as '.'.
    """
    folders: set[str] = set()
    for path in file_tree:
        parent = str(Path(path).parent)
        folders.add(parent if parent != "." else "(root)")
    return sorted(folders)


def _plain_text_file_tree(paths: list[str], level: str) -> str:
    """
    Generate a plain-text file tree as a fallback when Mermaid generation fails.
    Prefixes with a clear note so the frontend can display it as text, not Mermaid.
    """
    header = f"# Vega — Repository Structure ({level}-level view)\n\n"
    body = "\n".join(f"  {p}" for p in sorted(paths))
    return header + body
