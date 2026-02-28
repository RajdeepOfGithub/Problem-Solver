"""
agents/ops_mode/code_action.py
Vega — Phase 5: Code Action Agent

Execution agent for all file-level code changes. Lives in Ops Mode because it
performs real actions (create, modify, refactor files via GitHub API).

Pipeline:
  1. propose_action() — generates unified diff + explanation, returns for confirmation
  2. Safety gate (POST /action/confirm) — enforced by api/server.py
  3. execute_action() — only runs if confirmed=True with matching action_id

⚠️  SAFETY CONTRACT:
All write operations require a confirmed action_id. execute_action() will not
proceed without confirmed=True — this is a hard, non-bypassable constraint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    Path(__file__).parent.parent.parent / "prompts" / "ops_mode" / "code_action.txt"
)

_LOW_CONFIDENCE_THRESHOLD = 0.6


class CodeActionAgent:
    """
    Execution agent for file-level code changes.

    Usage::

        agent = CodeActionAgent(github_actions=github_actions_instance)

        # Step 1: Generate proposal (safe, read-only)
        proposal = await agent.propose_action(
            voice_text="Create a rate limiting middleware",
            file_tree=["api/server.py", ...],
            code_chunks=[...],
            session_context=[...],
        )
        # Returns dict with proposed_change, explanation, action_id, etc.

        # Step 2: Safety gate — POST /action/confirm must happen here

        # Step 3: Execute confirmed action
        result = await agent.execute_action(proposal, confirmed=True)
    """

    def __init__(self, github_actions: Optional[Any] = None) -> None:
        """
        Args:
            github_actions: Instance of the GitHubActions class from
                            actions/github_actions.py. If None, execute_action
                            will fail with a clear error message.
        """
        self._bedrock = boto3.client(
            "bedrock-runtime",
            region_name=AWS_REGION,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )
        self.github_actions = github_actions
        self._system_prompt: str = self._load_system_prompt()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def propose_action(
        self,
        voice_text: str,
        file_tree: List[str],
        code_chunks: List[Dict],
        session_context: List[Dict],
        root_cause: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Generate a proposed code action (create/modify/refactor).
        Does NOT execute — returns a proposal for voice confirmation.

        Args:
            voice_text:      The developer's transcribed voice request.
            file_tree:       All file paths in the indexed repo.
            code_chunks:     Relevant FAISS code chunks.
            session_context: Recent conversation turns.
            root_cause:      Root cause dict if triggered from incident fix pipeline.

        Returns schema matching AGENTS.md Code Action Agent output::

            {
                "status": "ok | cannot_generate",
                "action_type": "create_file | modify_file | refactor",
                "target_file": "path/to/file.py",
                "proposed_change": "unified diff string",
                "explanation": "Voice-ready, max 3 sentences",
                "confidence_score": 0.0-1.0,
                "warnings": [...],
                "proposed_pr_title": "...",
                "proposed_pr_body": "...",
                "action_id": "act_xxxxxxxx",
            }
        """
        prompt = self._build_proposal_prompt(
            voice_text, file_tree, code_chunks, session_context, root_cause
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
            logger.error("CodeActionAgent: Bedrock error (%s): %s", code, exc)
            return self._error_response(str(exc))
        except json.JSONDecodeError as exc:
            logger.error("CodeActionAgent: JSON parse error: %s", exc)
            return self._error_response(f"Model returned invalid JSON: {exc}")
        except Exception as exc:
            logger.error("CodeActionAgent: unexpected error in propose_action: %s", exc)
            return self._error_response(str(exc))

        # Assign a fresh action_id for safety gate tracking
        result["action_id"] = f"act_{uuid.uuid4().hex[:8]}"

        # Enforce low-confidence warning
        score = result.get("confidence_score", 0.0)
        if score < _LOW_CONFIDENCE_THRESHOLD:
            warnings = result.setdefault("warnings", [])
            already_warned = any(
                w.get("type") == "low_confidence" for w in warnings
            )
            if not already_warned:
                warnings.append({
                    "type": "low_confidence",
                    "description": (
                        f"Confidence is {score:.0%} — recommend manual review before merging."
                    ),
                })

        logger.info(
            "CodeActionAgent: proposal generated — action_id=%s type=%s target=%s confidence=%.2f",
            result.get("action_id"),
            result.get("action_type"),
            result.get("target_file"),
            score,
        )
        return result

    async def execute_action(
        self, action_result: Dict[str, Any], confirmed: bool
    ) -> Dict[str, Any]:
        """
        Execute a confirmed action via the GitHub API.

        ⚠️  SAFETY GATE: Will not execute unless confirmed=True.
        The action_id in action_result is used for audit logging.

        Args:
            action_result: The dict returned by propose_action().
            confirmed:     Must be True (set by POST /action/confirm).

        Returns::

            {
                "action_id": str,
                "status": "success | cancelled | failed",
                "message": str,
                "result": dict,  # only on success
            }
        """
        action_id = action_result.get("action_id", "unknown")

        if not confirmed:
            logger.info("CodeActionAgent: action %s cancelled by developer", action_id)
            return {
                "action_id": action_id,
                "status": "cancelled",
                "message": "Action cancelled by developer.",
            }

        if not self.github_actions:
            logger.error(
                "CodeActionAgent: execute_action called but github_actions not configured"
            )
            return {
                "action_id": action_id,
                "status": "failed",
                "message": "GitHub actions not configured. Provide github_actions= at init.",
            }

        action_type = action_result.get("action_type")
        target_file = action_result.get("target_file")
        proposed_change = action_result.get("proposed_change", "")
        pr_title = action_result.get("proposed_pr_title", f"Vega: {action_type} {target_file}")
        pr_body = action_result.get("proposed_pr_body", "Generated by Vega Code Action Agent.")

        try:
            if action_type == "create_file":
                content = self._extract_content_from_diff(proposed_change)
                result = await asyncio.to_thread(
                    self.github_actions.create_or_update_file,
                    path=target_file,
                    content=content,
                    message=pr_title,
                    branch="main",
                )

            elif action_type in ("modify_file", "refactor"):
                # Create a draft PR with the unified diff
                result = await asyncio.to_thread(
                    self.github_actions.create_draft_pr_with_diff,
                    title=pr_title,
                    body=pr_body,
                    diff=proposed_change,
                )

            else:
                return {
                    "action_id": action_id,
                    "status": "failed",
                    "message": f"Unknown action_type: {action_type!r}",
                }

            logger.info("CodeActionAgent: action %s executed successfully", action_id)
            return {
                "action_id": action_id,
                "status": "success",
                "result": result,
            }

        except Exception as exc:
            logger.error("CodeActionAgent: execute_action failed for %s: %s", action_id, exc)
            return {
                "action_id": action_id,
                "status": "failed",
                "message": str(exc),
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_proposal_prompt(
        self,
        voice_text: str,
        file_tree: List[str],
        code_chunks: List[Dict],
        session_context: List[Dict],
        root_cause: Optional[Dict],
    ) -> str:
        """Construct the full prompt for Nova Lite code generation."""
        parts: List[str] = [
            f"DEVELOPER REQUEST: {voice_text}",
            f"\nFILE TREE:\n" + "\n".join(f"  {f}" for f in file_tree[:50]),
        ]

        if code_chunks:
            parts.append("\nRELEVANT CODE CHUNKS:")
            for chunk in code_chunks[:5]:
                parts.append(
                    f"\n--- {chunk.get('file', 'unknown')} "
                    f"(lines {chunk.get('start_line', '?')}-{chunk.get('end_line', '?')}) ---"
                )
                parts.append(chunk.get("content", "")[:1500])

        if root_cause:
            parts.append("\nROOT CAUSE CONTEXT:")
            parts.append(json.dumps(root_cause, indent=2)[:2000])

        if session_context:
            parts.append("\nSESSION CONTEXT (recent turns):")
            for turn in session_context[-3:]:
                parts.append(
                    f"  [{turn.get('role', '?')}]: {str(turn.get('content', ''))[:200]}"
                )

        return "\n".join(parts)

    def _extract_content_from_diff(self, diff: str) -> str:
        """
        Extract new file content from a unified diff for create_file actions.
        Takes all lines beginning with '+' (excluding +++ header lines).
        """
        lines: List[str] = []
        for line in diff.split("\n"):
            if line.startswith("+") and not line.startswith("+++"):
                lines.append(line[1:])  # Strip the leading +
        return "\n".join(lines)

    def _load_system_prompt(self) -> str:
        """Load system prompt from file; fall back to minimal inline default."""
        try:
            return _PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("code_action.txt not found — using inline default")
            return (
                "You are Vega's Code Action Agent. Generate precise code changes. "
                "Return valid JSON only matching the required schema."
            )

    @staticmethod
    def _strip_fences(text: str) -> str:
        """Remove markdown code fences from Nova Lite output."""
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        return text.strip()

    @staticmethod
    def _error_response(message: str) -> Dict[str, Any]:
        """Return a standardized error response."""
        return {
            "status": "cannot_generate",
            "action_type": None,
            "target_file": None,
            "proposed_change": None,
            "explanation": f"Failed to generate action: {message}",
            "confidence_score": 0.0,
            "warnings": [{"type": "error", "description": message}],
            "proposed_pr_title": None,
            "proposed_pr_body": None,
            "action_id": f"act_{uuid.uuid4().hex[:8]}",
        }
