"""
agents/dev_mode/pr_review.py
Vega — PR Review Agent

Reviews a pull request diff against the existing codebase context —
correctness, test coverage, breaking changes, style. Returns a verdict
and actionable inline comments. All verdicts require voice confirmation
before any GitHub API call.

Model:  Amazon Nova 2 Lite via Bedrock converse API
Prompt: prompts/dev_mode/pr_review.txt
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
    Path(__file__).parent.parent.parent / "prompts" / "dev_mode" / "pr_review.txt"
)


class PRReviewAgent:
    """
    Reviews PR diffs for correctness, tests, breaking changes, and style.

    Usage::

        agent = PRReviewAgent()
        result = await agent.analyze(
            pr_diff="--- a/file.py\\n+++ b/file.py\\n...",
            pr_description="Fix auth token expiry bug",
            code_chunks=[...],
            original_query="Review PR 42",
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
        pr_diff: str,
        pr_description: str = "",
        code_chunks: List[Dict] | None = None,
        original_query: str = "",
    ) -> Dict[str, Any]:
        """
        Review a PR diff against codebase context.

        Args:
            pr_diff:        Unified diff string of the pull request.
            pr_description: PR title + description written by the author.
            code_chunks:    Retrieved context from knowledge base for modified files.
            original_query: The developer's original voice request.

        Returns:
            Dict matching pr_review.txt output schema:
            {status, verdict, summary, breaking_changes_detected, inline_comments,
             missing_tests, missing_test_note}
        """
        prompt = self._build_prompt(pr_diff, pr_description, code_chunks or [], original_query)

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
            logger.error("PRReviewAgent: Bedrock error (%s): %s", code, exc)
            return self._error_response(str(exc))
        except json.JSONDecodeError as exc:
            logger.error("PRReviewAgent: JSON parse error: %s", exc)
            return self._error_response(f"Model returned invalid JSON: {exc}")
        except Exception as exc:
            logger.error("PRReviewAgent: unexpected error: %s", exc)
            return self._error_response(str(exc))

        logger.info(
            "PRReviewAgent: review complete — verdict=%s, comments=%d, breaking=%s",
            result.get("verdict", "?"),
            len(result.get("inline_comments", [])),
            result.get("breaking_changes_detected", False),
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        pr_diff: str,
        pr_description: str,
        code_chunks: List[Dict],
        original_query: str,
    ) -> str:
        parts: List[str] = [f"ORIGINAL QUERY: {original_query}"]

        if pr_description:
            parts.append(f"\nPR DESCRIPTION:\n{pr_description[:2000]}")

        parts.append(f"\nPR DIFF:\n{pr_diff[:8000]}")

        if code_chunks:
            parts.append("\nCODE CHUNKS (context for modified files):")
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
            logger.warning("pr_review.txt not found — using inline default")
            return (
                "You are Vega's PR Review Agent. Review pull request diffs. "
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
            "verdict": "comment",
            "summary": f"PR review could not complete: {message}",
            "breaking_changes_detected": False,
            "inline_comments": [],
            "missing_tests": False,
            "missing_test_note": None,
        }
