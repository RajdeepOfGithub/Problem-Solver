"""
agents/dev_mode/architecture_analysis.py
Vega — Architecture Analysis Agent

Evaluates the structural design of a codebase — coupling, cohesion,
design patterns, scalability concerns. Returns actionable improvement
suggestions with priority.

Model:  Amazon Nova 2 Lite via Bedrock converse API
Prompt: prompts/dev_mode/architecture_analysis.txt
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
    Path(__file__).parent.parent.parent / "prompts" / "dev_mode" / "architecture_analysis.txt"
)


class ArchitectureAnalysisAgent:
    """
    Evaluates architectural health — coupling, cohesion, design patterns, scalability.

    Usage::

        agent = ArchitectureAnalysisAgent()
        result = await agent.analyze(
            code_chunks=[...],
            readme_content="...",
            diagram_chunks=[...],
            original_query="How's the architecture?",
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
        code_chunks: List[Dict],
        readme_content: str = "",
        diagram_chunks: List[Dict] | None = None,
        original_query: str = "",
    ) -> Dict[str, Any]:
        """
        Evaluate the architectural health of a codebase.

        Args:
            code_chunks:    List of code chunk dicts with file structure context.
            readme_content: Contents of the repository README (may be empty).
            diagram_chunks: Embedded architecture diagram context (may be empty).
            original_query: The developer's original voice request.

        Returns:
            Dict matching architecture_analysis.txt output schema:
            {status, overall_health, patterns_identified, concerns, suggestions, voice_summary}
        """
        prompt = self._build_prompt(code_chunks, readme_content, diagram_chunks or [], original_query)

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
            logger.error("ArchitectureAnalysisAgent: Bedrock error (%s): %s", code, exc)
            return self._error_response(str(exc))
        except json.JSONDecodeError as exc:
            logger.error("ArchitectureAnalysisAgent: JSON parse error: %s", exc)
            return self._error_response(f"Model returned invalid JSON: {exc}")
        except Exception as exc:
            logger.error("ArchitectureAnalysisAgent: unexpected error: %s", exc)
            return self._error_response(str(exc))

        logger.info(
            "ArchitectureAnalysisAgent: analysis complete — health=%s, concerns=%d",
            result.get("overall_health", "?"),
            len(result.get("concerns", [])),
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        code_chunks: List[Dict],
        readme_content: str,
        diagram_chunks: List[Dict],
        original_query: str,
    ) -> str:
        parts: List[str] = [f"ORIGINAL QUERY: {original_query}"]

        if readme_content:
            parts.append(f"\nREADME CONTENT:\n{readme_content[:3000]}")

        parts.append("\nCODE CHUNKS:")
        for chunk in code_chunks[:10]:
            parts.append(
                f"\n--- {chunk.get('file', 'unknown')} "
                f"(lines {chunk.get('start_line', '?')}-{chunk.get('end_line', '?')}) ---"
            )
            parts.append(chunk.get("content", "")[:2000])

        if diagram_chunks:
            parts.append("\nDIAGRAM CHUNKS:")
            for chunk in diagram_chunks[:5]:
                parts.append(chunk.get("content", "")[:1000])

        return "\n".join(parts)

    def _load_system_prompt(self) -> str:
        try:
            return _PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("architecture_analysis.txt not found — using inline default")
            return (
                "You are Vega's Architecture Analysis Agent. Evaluate codebase structure. "
                "Return valid JSON only matching the required schema."
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
    def _error_response(message: str) -> Dict[str, Any]:
        return {
            "status": "insufficient_context",
            "overall_health": 0,
            "patterns_identified": [],
            "concerns": [],
            "suggestions": [],
            "voice_summary": f"Architecture analysis could not complete: {message}",
        }
