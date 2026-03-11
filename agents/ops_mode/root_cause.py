"""
agents/ops_mode/root_cause.py
Vega — Root Cause Agent

Correlates parsed log events with codebase context to trace an incident
back to a specific file, function, or code change. Produces a confident,
evidence-backed root cause statement.

Model:  Amazon Nova 2 Lite via Bedrock converse API
Prompt: prompts/ops_mode/root_cause.txt
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
    Path(__file__).parent.parent.parent / "prompts" / "ops_mode" / "root_cause.txt"
)


class RootCauseAgent:
    """
    Correlates log events with code chunks to identify the root cause.

    Usage::

        agent = RootCauseAgent()
        result = await agent.analyze(
            log_summary={...},
            code_chunks=[...],
            incident={...},
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
        log_summary: Dict[str, Any],
        code_chunks: List[Dict],
        incident: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Identify the root cause by matching log events to code locations.

        Args:
            log_summary: Structured output from LogParserAgent.
            code_chunks: Retrieved code chunks from the knowledge base.
            incident:    Structured incident object.

        Returns:
            Dict matching root_cause.txt output schema:
            {status, root_cause_statement, confidence, evidence, suspected_commit,
             next_action, confidence_rationale}
        """
        prompt = self._build_prompt(log_summary, code_chunks, incident)

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
            logger.error("RootCauseAgent: Bedrock error (%s): %s", code, exc)
            return self._error_response(str(exc))
        except json.JSONDecodeError as exc:
            logger.error("RootCauseAgent: JSON parse error: %s", exc)
            return self._error_response(f"Model returned invalid JSON: {exc}")
        except Exception as exc:
            logger.error("RootCauseAgent: unexpected error: %s", exc)
            return self._error_response(str(exc))

        logger.info(
            "RootCauseAgent: analysis complete — confidence=%s",
            result.get("confidence", "unknown"),
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        log_summary: Dict[str, Any],
        code_chunks: List[Dict],
        incident: Dict[str, Any],
    ) -> str:
        parts: List[str] = [
            f"INCIDENT: {json.dumps(incident, indent=2)[:2000]}",
            f"\nLOG SUMMARY: {json.dumps(log_summary, indent=2)[:3000]}",
        ]

        if code_chunks:
            parts.append("\nCODE CHUNKS:")
            for chunk in code_chunks[:8]:
                parts.append(
                    f"\n--- {chunk.get('file', 'unknown')} "
                    f"(lines {chunk.get('start_line', '?')}-{chunk.get('end_line', '?')}) ---"
                )
                parts.append(chunk.get("content", "")[:1500])

        return "\n".join(parts)

    def _load_system_prompt(self) -> str:
        try:
            return _PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("root_cause.txt not found — using inline default")
            return (
                "You are Vega's Root Cause Agent. Correlate logs with code to find root causes. "
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
            "root_cause_statement": f"Root cause analysis could not complete: {message}",
            "confidence": "low",
            "evidence": [],
            "suspected_commit": None,
            "next_action": "generate_fix",
            "confidence_rationale": "Analysis failed due to an internal error.",
        }
