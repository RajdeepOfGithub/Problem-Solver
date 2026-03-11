"""
agents/ops_mode/incident.py
Vega — Incident Analysis Agent (first responder in Ops Mode)

Extracts service, time window, and severity from a developer's voice
description of a production incident. Outputs a structured incident object
for the Log Parsing Agent.

Model:  Amazon Nova 2 Lite via Bedrock converse API
Prompt: prompts/ops_mode/incident.txt
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
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
    Path(__file__).parent.parent.parent / "prompts" / "ops_mode" / "incident.txt"
)


class IncidentAnalysisAgent:
    """
    First responder in Ops Mode — extracts incident metadata from voice input.

    Usage::

        agent = IncidentAnalysisAgent()
        result = await agent.analyze(
            voice_text="My Lambda auth function is returning 500 errors",
            session_memory=[...],
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
        voice_text: str,
        session_memory: List[Dict] | None = None,
    ) -> Dict[str, Any]:
        """
        Analyze an incident description and produce a structured incident object.

        Args:
            voice_text:     The developer's transcribed voice description.
            session_memory: Prior conversation turns (may be empty).

        Returns:
            Dict matching incident.txt output schema:
            {status, clarifying_question, incident, next_action, voice_acknowledgement}
        """
        current_time = datetime.now(timezone.utc).isoformat()
        prompt = self._build_prompt(voice_text, session_memory or [], current_time)

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
            logger.error("IncidentAnalysisAgent: Bedrock error (%s): %s", code, exc)
            return self._error_response(str(exc))
        except json.JSONDecodeError as exc:
            logger.error("IncidentAnalysisAgent: JSON parse error: %s", exc)
            return self._error_response(f"Model returned invalid JSON: {exc}")
        except Exception as exc:
            logger.error("IncidentAnalysisAgent: unexpected error: %s", exc)
            return self._error_response(str(exc))

        logger.info(
            "IncidentAnalysisAgent: incident parsed — service=%s severity=%s",
            result.get("incident", {}).get("service", "unknown"),
            result.get("incident", {}).get("severity", "unknown"),
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        voice_text: str,
        session_memory: List[Dict],
        current_time: str,
    ) -> str:
        parts: List[str] = [
            f"VOICE TEXT: {voice_text}",
            f"\nCURRENT TIME (UTC): {current_time}",
        ]

        if session_memory:
            parts.append("\nSESSION MEMORY (recent turns):")
            for turn in session_memory[-5:]:
                parts.append(
                    f"  [{turn.get('role', '?')}]: {str(turn.get('content', ''))[:200]}"
                )

        return "\n".join(parts)

    def _load_system_prompt(self) -> str:
        try:
            return _PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("incident.txt not found — using inline default")
            return (
                "You are Vega's Incident Analysis Agent. Extract incident details from voice input. "
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
            "status": "needs_clarification",
            "clarifying_question": f"I encountered an error analyzing the incident: {message}. Could you describe the issue again?",
            "incident": None,
            "next_action": "retrieve_logs",
            "voice_acknowledgement": "I had trouble processing that. Could you describe the incident again?",
        }
