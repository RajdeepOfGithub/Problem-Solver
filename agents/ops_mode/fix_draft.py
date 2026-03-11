"""
agents/ops_mode/fix_draft.py
Vega — Fix Draft Agent (end of Ops Mode golden path)

Takes a root cause analysis and affected code chunks and generates a
proposed code fix as a unified diff. Includes voice-ready explanation
and confidence scoring. Does NOT call GitHub directly — PR creation
requires voice confirmation through the safety gate.

Model:  Amazon Nova 2 Lite via Bedrock converse API
Prompt: prompts/ops_mode/fix_draft.txt
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
    Path(__file__).parent.parent.parent / "prompts" / "ops_mode" / "fix_draft.txt"
)

_LOW_CONFIDENCE_THRESHOLD = 0.6


class FixDraftAgent:
    """
    Generates a proposed code fix as a unified diff from a root cause analysis.

    Usage::

        agent = FixDraftAgent()
        result = await agent.analyze(
            root_cause={...},
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
        root_cause: Dict[str, Any],
        code_chunks: List[Dict],
        incident: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Generate a proposed code fix for the identified root cause.

        Args:
            root_cause:  Structured output from RootCauseAgent.
            code_chunks: Code chunks containing the root cause location.
            incident:    Structured incident object.

        Returns:
            Dict matching fix_draft.txt output schema:
            {status, fix_diff, explanation, confidence_score, files_modified,
             warnings, proposed_pr_title, proposed_pr_body}
        """
        # Cap confidence if root cause confidence is low
        rc_confidence = root_cause.get("confidence", "low")

        prompt = self._build_prompt(root_cause, code_chunks, incident)

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
            logger.error("FixDraftAgent: Bedrock error (%s): %s", code, exc)
            return self._error_response(str(exc))
        except json.JSONDecodeError as exc:
            logger.error("FixDraftAgent: JSON parse error: %s", exc)
            return self._error_response(f"Model returned invalid JSON: {exc}")
        except Exception as exc:
            logger.error("FixDraftAgent: unexpected error: %s", exc)
            return self._error_response(str(exc))

        # Cap confidence_score at 0.5 if root cause was low confidence
        score = result.get("confidence_score", 0.0)
        if rc_confidence == "low" and score > 0.5:
            result["confidence_score"] = 0.5
            score = 0.5

        # Enforce low-confidence warning
        if score < _LOW_CONFIDENCE_THRESHOLD:
            warnings = result.setdefault("warnings", [])
            already_warned = any(w.get("type") == "low_confidence" for w in warnings)
            if not already_warned:
                warnings.append({
                    "type": "low_confidence",
                    "description": (
                        f"Confidence is {score:.0%} — recommend manual review before merging."
                    ),
                })

        logger.info(
            "FixDraftAgent: fix generated — confidence=%.2f files=%s",
            score,
            result.get("files_modified", []),
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        root_cause: Dict[str, Any],
        code_chunks: List[Dict],
        incident: Dict[str, Any],
    ) -> str:
        parts: List[str] = [
            f"INCIDENT: {json.dumps(incident, indent=2)[:2000]}",
            f"\nROOT CAUSE: {json.dumps(root_cause, indent=2)[:3000]}",
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
            logger.warning("fix_draft.txt not found — using inline default")
            return (
                "You are Vega's Fix Draft Agent. Generate minimal code fixes as unified diffs. "
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
            "status": "cannot_generate_fix",
            "fix_diff": None,
            "explanation": f"Fix generation could not complete: {message}",
            "confidence_score": 0.0,
            "files_modified": [],
            "warnings": [{"type": "error", "description": message}],
            "proposed_pr_title": None,
            "proposed_pr_body": None,
        }
