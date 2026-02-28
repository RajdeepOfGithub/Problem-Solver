"""
ingestion/doc_scanner.py
Vega — Phase 5: Project Intelligence Layer

Scans .md files in an indexed repository to extract planned components that
are mentioned in documentation but may not exist as code files yet, then
classifies every file in the tree as "built", "stub", or "planned".

Used by:
  - diagram/two_tone_generator.py  → color-code diagram nodes
  - agents/dev_mode/project_intelligence.py → gap analysis
  - api/server.py → GET /repo/diagram/{job_id}/two-tone
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Any

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOVA_LITE_MODEL_ID: str = os.getenv("NOVA_LITE_MODEL_ID", "us.amazon.nova-2-lite-v1:0")
AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")

# Minimum non-comment, non-blank lines for a file to be considered "built"
BUILT_LINE_THRESHOLD = 5

_EXTRACTION_SYSTEM_PROMPT = (
    "You are analyzing software project documentation. "
    "Return valid JSON only. No markdown, no prose."
)

_EXTRACTION_PROMPT_TEMPLATE = """\
You are analyzing a documentation file from a software project.
Extract all component names, file names, module names, or features that are PLANNED but not yet built.

Signals that something is planned (not yet built):
- "will build", "TODO", "planned", "phase X" (future phase), "next steps"
- "remaining", "future", "upcoming", "not yet implemented", "stub", "scaffold"
- Checklist items that are unchecked: "- [ ]"
- Any mention of a file path that is described as something to create in the future

For each planned component, return:
- name: short identifier (e.g. "auth_middleware", "log_parser_agent")
- expected_path: the file path where this would live (if mentioned or inferrable, else "")
- signal: the exact text fragment that indicates this is planned

Respond with valid JSON only. No markdown, no prose.
{{"planned_components": [...]}}

If no planned components are found, return: {{"planned_components": []}}

DOCUMENT PATH: {md_path}
DOCUMENT CONTENT:
{md_content}
"""

# Comment markers by file extension
_COMMENT_MARKERS: dict[str, list[str]] = {
    ".py":    ["#"],
    ".js":    ["//", "/*", "*", "*/"],
    ".ts":    ["//", "/*", "*", "*/"],
    ".tsx":   ["//", "/*", "*", "*/"],
    ".jsx":   ["//", "/*", "*", "*/"],
    ".java":  ["//", "/*", "*", "*/"],
    ".go":    ["//", "/*", "*", "*/"],
    ".rs":    ["//", "/*", "*", "*/"],
    ".rb":    ["#"],
    ".sh":    ["#"],
    ".yaml":  ["#"],
    ".yml":   ["#"],
    ".toml":  ["#"],
    ".css":   ["/*", "*", "*/"],
    ".html":  ["<!--", "-->"],
    ".sql":   ["--", "/*", "*", "*/"],
}


class DocScanner:
    """
    Scans .md files in a repo to extract planned components and classify
    each file in the tree as 'built', 'stub', or 'planned'.

    Usage::

        scanner = DocScanner()
        result = scanner.scan_repo(file_tree, md_contents, repo_path)
        # result["file_status"]  → {"api/server.py": "built", ...}
        # result["planned_components"] → [{"name": ..., "expected_path": ...}, ...]
    """

    def __init__(self) -> None:
        self._bedrock = boto3.client(
            "bedrock-runtime",
            region_name=AWS_REGION,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan_repo(
        self,
        file_tree: List[str],
        md_contents: Dict[str, str],
        repo_path: str,
    ) -> Dict[str, Any]:
        """
        Main entry point.

        Args:
            file_tree:    List of all file paths in the repo (relative to root).
            md_contents:  Dict mapping .md file paths → full text content.
            repo_path:    Local path to the cloned repo (for file content checks).

        Returns:
            {
                "planned_components": [...],
                "file_status": {"api/server.py": "built", ...},
                "doc_files_scanned": [...],
                "total_files": int,
                "built_count": int,
                "stub_count": int,
                "planned_count": int,
            }
        """
        file_tree_set = set(file_tree)

        # Step 1: Extract planned components from all .md files
        all_planned: List[Dict] = []
        for md_path, content in md_contents.items():
            try:
                found = self._extract_planned_components(md_path, content)
                all_planned.extend(found)
            except Exception as exc:
                logger.warning("DocScanner: extraction failed for %r: %s", md_path, exc)

        # Deduplicate by expected_path (keep first occurrence)
        seen_paths: set[str] = set()
        deduped_planned: List[Dict] = []
        for comp in all_planned:
            ep = comp.get("expected_path", "")
            key = ep or comp.get("name", "")
            if key and key not in seen_paths:
                seen_paths.add(key)
                deduped_planned.append(comp)

        # Step 2: Classify every file in the tree
        file_status: Dict[str, str] = {}
        for rel_path in file_tree:
            file_status[rel_path] = self._classify_file(rel_path, repo_path)

        # Step 3: Mark planned components that have no file as "planned"
        planned_paths = {c.get("expected_path", "") for c in deduped_planned if c.get("expected_path")}
        for path in planned_paths:
            if path not in file_status:
                file_status[path] = "planned"

        built_count = sum(1 for s in file_status.values() if s == "built")
        stub_count = sum(1 for s in file_status.values() if s == "stub")
        planned_count = sum(1 for s in file_status.values() if s == "planned")

        return {
            "planned_components": deduped_planned,
            "file_status": file_status,
            "doc_files_scanned": list(md_contents.keys()),
            "total_files": len(file_tree),
            "built_count": built_count,
            "stub_count": stub_count,
            "planned_count": planned_count,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_planned_components(
        self, md_path: str, md_content: str
    ) -> List[Dict]:
        """
        Call Nova Lite to extract planned component names from a single .md file.
        Returns list of {"name", "expected_path", "signal", "mentioned_in"}.
        Falls back to [] on any error.
        """
        # Truncate very large docs to avoid token overflow
        truncated = md_content[:8000]

        prompt = _EXTRACTION_PROMPT_TEMPLATE.format(
            md_path=md_path,
            md_content=truncated,
        )

        try:
            response = self._bedrock.converse(
                modelId=NOVA_LITE_MODEL_ID,
                system=[{"text": _EXTRACTION_SYSTEM_PROMPT}],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
            )
            raw: str = response["output"]["message"]["content"][0]["text"].strip()

            # Strip markdown fences if present
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw.rsplit("```", 1)[0]
            raw = raw.strip()

            parsed = json.loads(raw)
            components = parsed.get("planned_components", [])

            # Attach source doc
            for comp in components:
                comp["mentioned_in"] = md_path

            logger.debug(
                "DocScanner: %d planned components found in %r",
                len(components), md_path,
            )
            return components

        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            logger.warning("DocScanner: Bedrock error (%s) for %r: %s", code, md_path, exc)
            return []
        except json.JSONDecodeError as exc:
            logger.warning("DocScanner: JSON parse error for %r: %s", md_path, exc)
            return []
        except Exception as exc:
            logger.warning("DocScanner: unexpected error for %r: %s", md_path, exc)
            return []

    def _classify_file(self, file_path: str, repo_path: str) -> str:
        """
        Check if a file has real code (> BUILT_LINE_THRESHOLD non-comment lines),
        is a stub, or doesn't exist.

        Returns:
            "built"   — file exists with meaningful code
            "stub"    — file exists but is mostly empty/comments
        """
        abs_path = Path(repo_path) / file_path

        if not abs_path.exists():
            return "stub"  # File doesn't exist in the local clone

        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("DocScanner: could not read %r: %s", file_path, exc)
            return "stub"

        ext = Path(file_path).suffix.lower()
        real_lines = [
            line for line in content.splitlines()
            if line.strip() and not self._is_comment_line(line, ext)
        ]

        return "built" if len(real_lines) > BUILT_LINE_THRESHOLD else "stub"

    def _is_comment_line(self, line: str, file_ext: str) -> bool:
        """
        Determine if a stripped line is a comment based on file extension.
        Handles single-line comment markers; block comments treated conservatively.
        """
        stripped = line.strip()
        if not stripped:
            return True

        markers = _COMMENT_MARKERS.get(file_ext, ["#"])
        for marker in markers:
            if stripped.startswith(marker):
                return True

        return False


# ---------------------------------------------------------------------------
# Module-level convenience (used by server.py)
# ---------------------------------------------------------------------------

def scan_repo_chunks(
    chunks: List[Dict],
    file_tree: List[str],
    repo_path: str,
) -> Dict[str, Any]:
    """
    Convenience wrapper that extracts .md contents from indexed chunks
    and delegates to DocScanner.scan_repo().

    Args:
        chunks:    List of chunk dicts from the ingestion pipeline.
        file_tree: List of relative file paths in the repo.
        repo_path: Local path to the cloned repo.

    Returns:
        DocScanner.scan_repo() output dict.
    """
    # Reconstruct md_contents from chunks (reassemble each .md file's content)
    md_chunks: dict[str, list[str]] = {}
    for chunk in chunks:
        if chunk.get("language") == "markdown":
            path = chunk["file"]
            md_chunks.setdefault(path, []).append(chunk["content"])

    md_contents: Dict[str, str] = {
        path: "\n".join(parts) for path, parts in md_chunks.items()
    }

    scanner = DocScanner()
    return scanner.scan_repo(file_tree, md_contents, repo_path)
