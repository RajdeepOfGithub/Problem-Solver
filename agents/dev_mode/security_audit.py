"""
agents/dev_mode/security_audit.py
Vega — Security Audit Agent (PRIMARY Dev Mode golden path agent)

Scans code chunks and dependency files for OWASP Top 10 vulnerabilities,
exposed secrets, insecure configurations, and known vulnerable dependency
versions. Voice summary covers CRITICAL + HIGH only.

Model:  Amazon Nova 2 Lite via Bedrock converse API
Prompt: prompts/dev_mode/security_audit.txt
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
    Path(__file__).parent.parent.parent / "prompts" / "dev_mode" / "security_audit.txt"
)


class SecurityAuditAgent:
    """
    Scans indexed code for security vulnerabilities (OWASP Top 10).

    Usage::

        agent = SecurityAuditAgent()
        result = await agent.analyze(
            code_chunks=[...],
            dependency_files="requirements.txt contents...",
            original_query="Run a security audit on the auth module",
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
        dependency_files: str = "",
        original_query: str = "",
    ) -> Dict[str, Any]:
        """
        Run a security audit on the provided code chunks.

        Args:
            code_chunks:      List of code chunk dicts with {file, start_line, end_line, content}.
            dependency_files: Raw text of requirements.txt / package.json (may be empty).
            original_query:   The developer's original voice request.

        Returns:
            Dict matching the security_audit.txt output schema:
            {status, vulnerability_count, findings, voice_summary}
        """
        prompt = self._build_prompt(code_chunks, dependency_files, original_query)

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
            logger.error("SecurityAuditAgent: Bedrock error (%s): %s", code, exc)
            return self._error_response(str(exc))
        except json.JSONDecodeError as exc:
            logger.error("SecurityAuditAgent: JSON parse error: %s", exc)
            return self._error_response(f"Model returned invalid JSON: {exc}")
        except Exception as exc:
            logger.error("SecurityAuditAgent: unexpected error: %s", exc)
            return self._error_response(str(exc))

        logger.info(
            "SecurityAuditAgent: audit complete — %s",
            result.get("vulnerability_count", {}),
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        code_chunks: List[Dict],
        dependency_files: str,
        original_query: str,
    ) -> str:
        parts: List[str] = [f"ORIGINAL QUERY: {original_query}"]

        parts.append("\nCODE CHUNKS:")
        for chunk in code_chunks[:10]:
            parts.append(
                f"\n--- {chunk.get('file', 'unknown')} "
                f"(lines {chunk.get('start_line', '?')}-{chunk.get('end_line', '?')}) ---"
            )
            parts.append(chunk.get("content", "")[:2000])

        if dependency_files:
            parts.append(f"\nDEPENDENCY FILES:\n{dependency_files[:3000]}")

        return "\n".join(parts)

    def _load_system_prompt(self) -> str:
        try:
            return _PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("security_audit.txt not found — using inline default")
            return (
                "You are Vega's Security Audit Agent. Scan code for OWASP Top 10 vulnerabilities. "
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
            "vulnerability_count": {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0},
            "findings": [],
            "voice_summary": f"Security audit could not complete: {message}",
        }
