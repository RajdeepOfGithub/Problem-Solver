"""
agents/dev_mode/codebase_explorer.py
Vega — Codebase Explorer Agent

Guides developers through an unfamiliar codebase via voice. Produces an
ordered, sentence-by-sentence walkthrough paired with diagram node IDs for
real-time highlighting. Runs automatically on session start for repo overview,
and on-demand for specific flow questions.

Model:  Amazon Nova 2 Lite via Bedrock converse API
Prompt: prompts/dev_mode/codebase_explorer.txt
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

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

_PROMPT_PATH: Path = (
    Path(__file__).parent.parent.parent / "prompts" / "dev_mode" / "codebase_explorer.txt"
)

_MAX_FILES = 100
_MAX_OVERVIEW_SENTENCES = 8
_MAX_FLOW_SENTENCES = 6


class CodebaseExplorerAgent:
    """
    Guides developers through an unfamiliar codebase via voice.

    Produces an ordered walkthrough where each sentence is paired with
    diagram node IDs to highlight in real time as Nova Sonic speaks.

    Usage::

        agent = CodebaseExplorerAgent()
        result = await agent.analyze(
            voice_query="overview",
            file_tree=["api/server.py", ...],
            import_graph={"api/server.py": ["agents/orchestrator.py"]},
            code_chunks=[...],
            diagram_level="file",
            diagram_node_ids=["api_server_py", "agents_orchestrator_py"],
        )
    """

    def __init__(self) -> None:
        self._bedrock = boto3.client(
            "bedrock-runtime",
            region_name=AWS_REGION,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )
        self._system_prompt: str = self._load_system_prompt()

    async def analyze(
        self,
        voice_query: str,
        file_tree: List[str],
        import_graph: Dict[str, List[str]],
        code_chunks: List[Dict],
        diagram_level: str = "file",
        diagram_node_ids: List[str] | None = None,
    ) -> Dict[str, Any]:
        """
        Produce a voice walkthrough of the codebase or a specific flow.

        Args:
            voice_query:      "overview" for auto session-start, or a specific question.
            file_tree:        Complete list of all files/folders in the repo.
            import_graph:     {file_path: [imported_file_paths]} mapping.
            code_chunks:      FAISS-retrieved chunks relevant to the query.
            diagram_level:    "file" or "folder".
            diagram_node_ids: All valid node IDs in the current diagram.

        Returns:
            Dict matching codebase_explorer.txt output schema:
            {status, repo_summary, diagram_level, walkthrough}
        """
        diagram_node_ids = diagram_node_ids or []

        # Scope constraint: repos over 100 files are rejected
        if len(file_tree) > _MAX_FILES:
            return {
                "status": "repo_too_large",
                "repo_summary": "",
                "diagram_level": diagram_level,
                "walkthrough": [],
            }

        prompt = self._build_prompt(
            voice_query, file_tree, import_graph, code_chunks,
            diagram_level, diagram_node_ids,
        )

        try:
            response = await asyncio.to_thread(
                self._bedrock.converse,
                modelId=NOVA_LITE_MODEL_ID,
                system=[{"text": self._system_prompt}],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
            )
            raw: str = response["output"]["message"]["content"][0]["text"].strip()
            raw = self._strip_fences(raw)
            result: Dict[str, Any] = json.loads(raw)

        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            logger.error("CodebaseExplorerAgent: Bedrock error (%s): %s", code, exc)
            return self._error_response(diagram_level)
        except json.JSONDecodeError as exc:
            logger.error("CodebaseExplorerAgent: JSON parse error: %s", exc)
            return self._error_response(diagram_level)
        except Exception as exc:
            logger.error("CodebaseExplorerAgent: unexpected error: %s", exc)
            return self._error_response(diagram_level)

        # Post-process: enforce node ID validity and sentence limits
        result = self._validate_output(result, diagram_node_ids, voice_query, diagram_level)

        logger.info(
            "CodebaseExplorerAgent: walkthrough generated — %d sentences, level=%s",
            len(result.get("walkthrough", [])),
            result.get("diagram_level", diagram_level),
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        voice_query: str,
        file_tree: List[str],
        import_graph: Dict[str, List[str]],
        code_chunks: List[Dict],
        diagram_level: str,
        diagram_node_ids: List[str],
    ) -> str:
        parts: List[str] = [
            f"VOICE QUERY: {voice_query}",
            f"\nDIAGRAM LEVEL: {diagram_level}",
            f"\nDIAGRAM NODE IDS (only use these in highlighted_nodes):\n{json.dumps(diagram_node_ids)}",
            f"\nFILE TREE ({len(file_tree)} files):",
        ]
        for f in file_tree[:80]:
            parts.append(f"  {f}")

        if import_graph:
            parts.append("\nIMPORT GRAPH (file → imports):")
            for src, targets in list(import_graph.items())[:40]:
                if targets:
                    parts.append(f"  {src} → {', '.join(targets[:5])}")
        else:
            # For repos with no import edges (e.g. notebook/binary repos like Sentinel),
            # Nova Lite should infer workflow order from README content in code_chunks.
            parts.append("\nIMPORT GRAPH: No import edges detected. Infer workflow order from README and file naming conventions.")

        if code_chunks:
            parts.append("\nCODE CHUNKS (relevant context):")
            for chunk in code_chunks[:8]:
                parts.append(
                    f"\n--- {chunk.get('file', 'unknown')} "
                    f"(lines {chunk.get('start_line', '?')}-{chunk.get('end_line', '?')}) ---"
                )
                parts.append(chunk.get("content", "")[:1500])

        return "\n".join(parts)

    def _validate_output(
        self,
        result: Dict[str, Any],
        diagram_node_ids: List[str],
        voice_query: str,
        diagram_level: str,
    ) -> Dict[str, Any]:
        """Post-process: strip invalid node IDs, enforce sentence limits."""
        valid_ids = set(diagram_node_ids)
        walkthrough = result.get("walkthrough", [])

        # Filter highlighted_nodes to only valid IDs
        for step in walkthrough:
            nodes = step.get("highlighted_nodes", [])
            step["highlighted_nodes"] = [n for n in nodes if n in valid_ids]

        # Enforce sentence count limits
        is_overview = voice_query.strip().lower() == "overview"
        max_sentences = _MAX_OVERVIEW_SENTENCES if is_overview else _MAX_FLOW_SENTENCES
        result["walkthrough"] = walkthrough[:max_sentences]

        # Ensure diagram_level is preserved
        result["diagram_level"] = result.get("diagram_level", diagram_level)

        return result

    def _load_system_prompt(self) -> str:
        try:
            return _PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("codebase_explorer.txt not found — using inline default")
            return (
                "You are Vega's Codebase Explorer Agent. Produce a voice walkthrough "
                "of the codebase. Return valid JSON only matching the required schema."
            )

    @staticmethod
    def _strip_fences(text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        return text.strip()

    @staticmethod
    def _error_response(diagram_level: str) -> Dict[str, Any]:
        return {
            "status": "insufficient_context",
            "repo_summary": "",
            "diagram_level": diagram_level,
            "walkthrough": [],
        }
