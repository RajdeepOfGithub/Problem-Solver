"""
agents/orchestrator.py
Vega — Phase 5: Orchestrator Agent (Mode System)

Central routing brain of the Vega multi-agent system. Receives every
transcribed voice input, classifies intent via Amazon Nova Lite (Bedrock
converse API), maintains per-session rolling 10-turn memory, and dispatches
to the correct specialised agent pipeline.

Phase 5 updates:
  - Extended intent taxonomy with Dev→Ops auto-switch support
  - New intents: dev_explore, dev_review, dev_build, ops_code_action
  - Fast-path keyword detection for execution intents (Dev→Ops switch)
  - mode_switch field in classify_intent output
  - per-session current_mode tracking

Intent taxonomy (Phase 5):
  dev_explore     | dev_review     | dev_build
  ops_incident    | ops_code_action| ops_followup | ambiguous
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")
# Cross-region inference profile required for converse API
_DEFAULT_MODEL_ID: str = "us.amazon.nova-2-lite-v1:0"

_PROMPT_PATH: Path = Path(__file__).parent.parent / "prompts" / "orchestrator.txt"

# Phase 5 intent taxonomy — replaces Phase 4 mode list
_AVAILABLE_INTENTS: list[str] = [
    "dev_explore",       # Codebase exploration, overview, walkthrough
    "dev_review",        # Code review, security audit, architecture analysis
    "dev_build",         # "What should I build next?" gap analysis
    "ops_incident",      # Production incident investigation
    "ops_code_action",   # Direct code change request → Dev→Ops switch
    "ops_followup",      # Follow-up on previous finding
    "ambiguous",         # Cannot classify → ask clarifying question
]

# Legacy alias for backwards compat with Phase 4 server code
_AVAILABLE_MODES = _AVAILABLE_INTENTS

# Fast-path keyword matching: any of these → ops_code_action (Dev→Ops switch)
_OPS_SWITCH_KEYWORDS: list[str] = [
    "create a new",
    "add a file",
    "add file",
    "write the code",
    "write a new",
    "implement this",
    "implement that",
    "scaffold",
    "generate the code",
    "build this",
    "build that",
    "build a",
    "fix this",
    "fix that",
    "change this",
    "change the code",
    "refactor this",
    "refactor the",
    "modify the code",
    "update the code",
]

_SAFE_FALLBACK: dict = {
    "voice_response": "I encountered an issue processing your request. Please try again.",
    "actions_proposed": [],
    "requires_confirmation": False,
}

_CLASSIFY_FALLBACK: dict = {
    "intent": "ambiguous",
    "confidence": 0.0,
    "clarifying_question": "Could you clarify what you need help with?",
    "context_summary": "",        # filled in at call time
    "route_to": "clarify",
    "mode_switch": None,
}


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class OrchestratorError(Exception):
    """Raised when a non-recoverable Orchestrator failure occurs."""


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

class OrchestratorAgent:
    """
    Central routing agent for Vega.

    Responsibilities:
    - Classify intent of incoming voice text via Nova Lite.
    - Maintain per-session rolling context window (10 turns).
    - Dispatch classified intents to the appropriate agent pipeline.

    Usage::

        agent = OrchestratorAgent()
        result = agent.process_turn("session-abc", "My Lambda is returning 500 errors")
        print(result["voice_response"])
    """

    def __init__(self) -> None:
        """
        Initialise the OrchestratorAgent.

        Loads the system prompt from prompts/orchestrator.txt, creates a
        Bedrock runtime client, and prepares the in-memory session store.

        Raises:
            FileNotFoundError: If prompts/orchestrator.txt does not exist.
            OrchestratorError: If the Bedrock client cannot be created.
        """
        # Load system prompt
        if not _PROMPT_PATH.exists():
            raise FileNotFoundError(
                f"Orchestrator system prompt not found at: {_PROMPT_PATH}. "
                "Ensure prompts/orchestrator.txt exists relative to the vega/ package root."
            )
        self._system_prompt: str = _PROMPT_PATH.read_text(encoding="utf-8")
        logger.debug("Loaded orchestrator prompt (%d chars) from %s", len(self._system_prompt), _PROMPT_PATH)

        # Nova Lite model ID
        self._model_id: str = os.getenv("NOVA_LITE_MODEL_ID", _DEFAULT_MODEL_ID)

        # Bedrock client
        try:
            self._bedrock = boto3.client(
                "bedrock-runtime",
                region_name=AWS_REGION,
                aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            )
        except Exception as exc:
            raise OrchestratorError(f"Failed to create Bedrock client: {exc}") from exc

        # Per-session memory store: session_id → list of turn dicts
        self._sessions: dict[str, list[dict]] = {}

        # Per-session mode tracking: session_id → "dev" | "ops"
        self._session_modes: dict[str, str] = {}

        logger.info(
            "OrchestratorAgent ready — model=%s region=%s", self._model_id, AWS_REGION
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify_intent(
        self,
        voice_text: str,
        session_memory: list[dict],
        current_mode: str = "dev",
    ) -> dict:
        """
        Classify the intent of a voice input using Nova Lite via the converse API.

        Phase 5: checks for Dev→Ops execution keywords first (fast path).
        Sends the voice text and the last 10 turns of session memory to Nova Lite
        as a structured JSON user message. Parses the model's JSON response and
        validates the required keys. Strips markdown code fences before parsing.

        Args:
            voice_text:     Transcribed voice input from the developer.
            session_memory: Current session memory list (may be empty).
            current_mode:   The current session mode ("dev" or "ops").

        Returns:
            Dict with keys:
                - intent              (str)
                - confidence          (float, 0.0–1.0)
                - clarifying_question (str or None)
                - context_summary     (str)
                - route_to            (str: 'dev_mode' | 'ops_mode' | 'clarify')
                - mode_switch         (dict {"from": ..., "to": ...} or None)

        Raises:
            OrchestratorError: If the Bedrock converse API call fails.
        """
        # ── Phase 5 fast path: detect execution intent via keyword matching ──
        lower_text = voice_text.lower()
        for keyword in _OPS_SWITCH_KEYWORDS:
            if keyword in lower_text:
                logger.info(
                    "classify_intent: fast-path ops_code_action match — keyword=%r", keyword
                )
                mode_switch = (
                    {"from": "dev", "to": "ops"} if current_mode == "dev" else None
                )
                return {
                    "intent": "ops_code_action",
                    "confidence": 0.85,
                    "clarifying_question": None,
                    "context_summary": (
                        f"Developer wants to {keyword} — switching to action mode."
                    ),
                    "route_to": "ops_mode",
                    "mode_switch": mode_switch,
                }

        # ── Fast path: return-to-dev keywords ────────────────────────────────
        return_keywords = ["go back", "done with ops", "return to dev", "back to dev", "cancel action"]
        for kw in return_keywords:
            if kw in lower_text and current_mode == "ops":
                return {
                    "intent": "ops_followup",
                    "confidence": 0.9,
                    "clarifying_question": None,
                    "context_summary": "Returning to Dev Mode.",
                    "route_to": "dev_mode",
                    "mode_switch": {"from": "ops", "to": "dev"},
                }

        user_message = json.dumps({
            "voice_text": voice_text,
            "session_memory": session_memory[-10:],
            "current_mode": current_mode,
            "available_intents": _AVAILABLE_INTENTS,
        })

        try:
            response = self._bedrock.converse(
                modelId=self._model_id,
                system=[{"text": self._system_prompt}],
                messages=[{"role": "user", "content": [{"text": user_message}]}],
            )
            response_text: str = response["output"]["message"]["content"][0]["text"]

        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            raise OrchestratorError(
                f"Bedrock converse failed ({code}): {exc}"
            ) from exc
        except Exception as exc:
            raise OrchestratorError(f"Unexpected Bedrock error: {exc}") from exc

        # Strip markdown fences if present
        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            # Remove the opening fence line (e.g. ```json)
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
        cleaned = cleaned.strip()

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.error(
                "classify_intent: JSON parse failed — raw response: %r — error: %s",
                response_text[:300], exc,
            )
            fallback = dict(_CLASSIFY_FALLBACK)
            fallback["context_summary"] = voice_text[:100]
            return fallback

        # Validate required keys
        required = {"intent", "confidence", "clarifying_question", "context_summary", "route_to"}
        missing = required - parsed.keys()
        if missing:
            logger.warning(
                "classify_intent: response missing keys %s — using fallback", missing
            )
            fallback = dict(_CLASSIFY_FALLBACK)
            fallback["context_summary"] = voice_text[:100]
            return fallback

        # Ensure mode_switch is always present (Nova Lite may omit it)
        if "mode_switch" not in parsed:
            parsed["mode_switch"] = None

        # Emit mode_switch for ops→dev transitions when model detects it
        if parsed.get("route_to") == "dev_mode" and current_mode == "ops":
            if not parsed.get("mode_switch"):
                parsed["mode_switch"] = {"from": "ops", "to": "dev"}
        elif parsed.get("route_to") == "ops_mode" and current_mode == "dev":
            if not parsed.get("mode_switch"):
                parsed["mode_switch"] = {"from": "dev", "to": "ops"}

        logger.debug(
            "classify_intent: intent=%r confidence=%.2f route_to=%r mode_switch=%r",
            parsed["intent"], parsed["confidence"], parsed["route_to"],
            parsed.get("mode_switch"),
        )
        return parsed

    async def dispatch_to_agents(self, intent: str, context: dict) -> dict:  # noqa: C901
        """
        Route an intent to the appropriate agent pipeline.

        Dispatches to real agent implementations and returns structured
        responses including voice_response, actions_proposed, and
        requires_confirmation.

        Args:
            intent:  Classified intent string from classify_intent().
            context: Dict containing voice_text, classification, session_memory.

        Returns:
            Response dict with voice_response, actions_proposed, requires_confirmation.
        """
        voice_text = context.get("voice_text", "")
        session_memory = context.get("session_memory", [])

        # ── dev_explore ──────────────────────────────────────────────────────
        if intent == "dev_explore":
            logger.info("dispatch_to_agents: spawning CodebaseExplorerAgent")
            try:
                from agents.dev_mode.codebase_explorer import CodebaseExplorerAgent
                agent = CodebaseExplorerAgent()
                result = await agent.analyze(
                    voice_query=voice_text or "overview",
                    file_tree=context.get("file_tree", []),
                    import_graph=context.get("import_graph", {}),
                    code_chunks=context.get("code_chunks", []),
                    diagram_level=context.get("diagram_level", "file"),
                    diagram_node_ids=context.get("diagram_node_ids", []),
                )
                walkthrough = result.get("walkthrough", [])
                voice_parts = [step.get("sentence", "") for step in walkthrough if step.get("sentence")]
                voice_response = " ".join(voice_parts) if voice_parts else result.get("repo_summary", "Here's an overview of the codebase.")
                return {
                    "voice_response": voice_response,
                    "walkthrough": walkthrough,
                    "repo_summary": result.get("repo_summary", ""),
                    "diagram_level": result.get("diagram_level", "file"),
                    "actions_proposed": [],
                    "requires_confirmation": False,
                }
            except Exception as exc:
                logger.error("dispatch_to_agents: CodebaseExplorerAgent failed: %s", exc)
                return {
                    "voice_response": "Let me walk you through the codebase. I'll give you an overview of the main modules and how they connect.",
                    "actions_proposed": [],
                    "requires_confirmation": False,
                }

        # ── dev_review (security audit) ──────────────────────────────────────
        if intent == "dev_review":
            logger.info("dispatch_to_agents: routing to SecurityAuditAgent")
            try:
                from agents.dev_mode.security_audit import SecurityAuditAgent
                agent = SecurityAuditAgent()
                code_chunks = context.get("code_chunks", [])
                dependency_files = context.get("dependency_files", "")
                result = await agent.analyze(
                    code_chunks=code_chunks,
                    dependency_files=dependency_files,
                    original_query=voice_text,
                )
                voice_summary = result.get("voice_summary", "Security audit complete.")
                return {
                    "voice_response": voice_summary,
                    "findings": result.get("findings", []),
                    "vulnerability_count": result.get("vulnerability_count", {}),
                    "actions_proposed": [],
                    "requires_confirmation": False,
                }
            except Exception as exc:
                logger.error("dispatch_to_agents: SecurityAuditAgent failed: %s", exc)
                return {
                    "voice_response": "I encountered an error running the security audit. Please try again.",
                    "findings": [],
                    "actions_proposed": [],
                    "requires_confirmation": False,
                }

        # ── dev_build ────────────────────────────────────────────────────────
        if intent == "dev_build":
            logger.info("dispatch_to_agents: spawning ProjectIntelligenceAgent")
            return {
                "voice_response": (
                    "Analyzing your project's documentation against the codebase. "
                    "I'll identify gaps and recommend what to build next."
                ),
                "actions_proposed": [],
                "requires_confirmation": False,
            }

        # ── ops_code_action ──────────────────────────────────────────────────
        if intent == "ops_code_action":
            logger.info("dispatch_to_agents: spawning CodeActionAgent — mode switched to ops")
            try:
                from agents.ops_mode.code_action import CodeActionAgent
                agent = CodeActionAgent()
                code_chunks = context.get("code_chunks", [])
                file_tree = context.get("file_tree", [])
                result = await agent.propose_action(
                    voice_text=voice_text,
                    file_tree=file_tree,
                    code_chunks=code_chunks,
                    session_context=session_memory,
                )
                mode_switch = context.get("classification", {}).get("mode_switch")
                explanation = result.get("explanation", "Code action proposed.")
                actions = []
                if result.get("status") == "ok" and result.get("action_id"):
                    actions.append({
                        "action_id": result["action_id"],
                        "type": result.get("action_type", "modify_file"),
                        "description": explanation,
                    })
                return {
                    "voice_response": explanation,
                    "actions_proposed": actions,
                    "requires_confirmation": bool(actions),
                    "mode_switch": mode_switch,
                    "code_action_result": result,
                }
            except Exception as exc:
                logger.error("dispatch_to_agents: CodeActionAgent failed: %s", exc)
                mode_switch = context.get("classification", {}).get("mode_switch")
                return {
                    "voice_response": "I encountered an error generating the code action. Please try again.",
                    "actions_proposed": [],
                    "requires_confirmation": False,
                    "mode_switch": mode_switch,
                }

        # ── dev_security_audit ─────────────────────────────────────────────
        if intent == "dev_security_audit":
            logger.info("dispatch_to_agents: routing dev_security_audit → SecurityAuditAgent")
            try:
                from agents.dev_mode.security_audit import SecurityAuditAgent
                agent = SecurityAuditAgent()
                code_chunks = context.get("code_chunks", [])
                result = await agent.analyze(
                    code_chunks=code_chunks,
                    original_query=voice_text,
                )
                return {
                    "voice_response": result.get("voice_summary", "Security audit complete."),
                    "findings": result.get("findings", []),
                    "vulnerability_count": result.get("vulnerability_count", {}),
                    "actions_proposed": [],
                    "requires_confirmation": False,
                }
            except Exception as exc:
                logger.error("dispatch_to_agents: SecurityAuditAgent failed: %s", exc)
                return {
                    "voice_response": "I encountered an error running the security audit. Please try again.",
                    "actions_proposed": [],
                    "requires_confirmation": False,
                }

        # ── dev_code_review ────────────────────────────────────────────────
        if intent == "dev_code_review":
            logger.info("dispatch_to_agents: routing dev_code_review → CodeReviewAgent")
            try:
                from agents.dev_mode.code_review import CodeReviewAgent
                agent = CodeReviewAgent()
                code_chunks = context.get("code_chunks", [])
                result = await agent.analyze(
                    code_chunks=code_chunks,
                    original_query=voice_text,
                    session_context=str(session_memory[-3:]) if session_memory else "",
                )
                return {
                    "voice_response": result.get("summary", "Code review complete."),
                    "findings": result.get("findings", []),
                    "files_reviewed": result.get("files_reviewed", []),
                    "complexity_score": result.get("complexity_score", 0),
                    "actions_proposed": [],
                    "requires_confirmation": False,
                }
            except Exception as exc:
                logger.error("dispatch_to_agents: CodeReviewAgent failed: %s", exc)
                return {
                    "voice_response": "I encountered an error running the code review. Please try again.",
                    "actions_proposed": [],
                    "requires_confirmation": False,
                }

        # ── dev_architecture ───────────────────────────────────────────────
        if intent == "dev_architecture":
            logger.info("dispatch_to_agents: routing dev_architecture → ArchitectureAnalysisAgent")
            try:
                from agents.dev_mode.architecture_analysis import ArchitectureAnalysisAgent
                agent = ArchitectureAnalysisAgent()
                code_chunks = context.get("code_chunks", [])
                readme_content = context.get("readme_content", "")
                result = await agent.analyze(
                    code_chunks=code_chunks,
                    readme_content=readme_content,
                    diagram_chunks=context.get("diagram_chunks"),
                    original_query=voice_text,
                )
                return {
                    "voice_response": result.get("voice_summary", "Architecture analysis complete."),
                    "overall_health": result.get("overall_health", 0),
                    "patterns_identified": result.get("patterns_identified", []),
                    "concerns": result.get("concerns", []),
                    "suggestions": result.get("suggestions", []),
                    "actions_proposed": [],
                    "requires_confirmation": False,
                }
            except Exception as exc:
                logger.error("dispatch_to_agents: ArchitectureAnalysisAgent failed: %s", exc)
                return {
                    "voice_response": "I encountered an error running the architecture analysis. Please try again.",
                    "actions_proposed": [],
                    "requires_confirmation": False,
                }

        # ── dev_pr_review ──────────────────────────────────────────────────
        if intent == "dev_pr_review":
            logger.info("dispatch_to_agents: routing dev_pr_review → PRReviewAgent")
            try:
                from agents.dev_mode.pr_review import PRReviewAgent
                agent = PRReviewAgent()
                code_chunks = context.get("code_chunks", [])
                result = await agent.analyze(
                    pr_diff=context.get("pr_diff", ""),
                    pr_description=context.get("pr_description", ""),
                    code_chunks=code_chunks,
                    original_query=voice_text,
                )
                verdict = result.get("verdict", "comment")
                actions = []
                needs_confirm = False
                if verdict in ("approve", "request_changes"):
                    actions.append({
                        "action_id": f"pr_review_{verdict}",
                        "type": "pr_review",
                        "description": f"Post PR review with verdict: {verdict}",
                        "verdict": verdict,
                    })
                    needs_confirm = True
                return {
                    "voice_response": result.get("summary", "PR review complete."),
                    "verdict": verdict,
                    "breaking_changes_detected": result.get("breaking_changes_detected", False),
                    "inline_comments": result.get("inline_comments", []),
                    "missing_tests": result.get("missing_tests", False),
                    "actions_proposed": actions,
                    "requires_confirmation": needs_confirm,
                }
            except Exception as exc:
                logger.error("dispatch_to_agents: PRReviewAgent failed: %s", exc)
                return {
                    "voice_response": "I encountered an error running the PR review. Please try again.",
                    "actions_proposed": [],
                    "requires_confirmation": False,
                }

        # ── ops_incident ─────────────────────────────────────────────────────
        if intent == "ops_incident":
            logger.info("dispatch_to_agents: spawning IncidentAnalysisAgent")
            try:
                from agents.ops_mode.incident import IncidentAnalysisAgent
                agent = IncidentAnalysisAgent()
                result = await agent.analyze(
                    voice_text=voice_text,
                    session_memory=session_memory,
                )
                voice_ack = result.get("voice_acknowledgement", "Incident received. Starting investigation.")
                return {
                    "voice_response": voice_ack,
                    "incident": result.get("incident"),
                    "actions_proposed": [],
                    "requires_confirmation": False,
                }
            except Exception as exc:
                logger.error("dispatch_to_agents: IncidentAnalysisAgent failed: %s", exc)
                return {
                    "voice_response": "Incident received. I'm retrieving logs and starting root cause analysis.",
                    "incident": None,
                    "actions_proposed": [],
                    "requires_confirmation": False,
                }

        # ── ops_followup ─────────────────────────────────────────────────────
        if intent == "ops_followup":
            logger.info("dispatch_to_agents: ops follow-up")
            return {
                "voice_response": "Got it. Following up on the previous analysis.",
                "actions_proposed": [],
                "requires_confirmation": False,
            }

        # ── ambiguous ────────────────────────────────────────────────────────
        if intent == "ambiguous":
            clarifying = (
                context.get("classification", {}).get("clarifying_question")
                or "Could you clarify what you need help with?"
            )
            logger.info("dispatch_to_agents: intent ambiguous — returning clarifying question")
            return {
                "voice_response": clarifying,
                "actions_proposed": [],
                "requires_confirmation": False,
            }

        # Unknown intent — defensive default
        logger.warning("dispatch_to_agents: unknown intent %r — returning generic stub", intent)
        return {
            "voice_response": (
                f"I received your request but could not determine the right pipeline for '{intent}'. "
                "Could you rephrase what you need?"
            ),
            "actions_proposed": [],
            "requires_confirmation": False,
        }

    async def process_turn(self, session_id: str, voice_text: str) -> dict:
        """
        Process a single voice turn end-to-end.

        Loads session memory, classifies intent, routes to the correct pipeline,
        updates rolling memory, and returns a response dict. This method NEVER
        raises — all exceptions are caught, logged, and surfaced as a safe
        fallback voice response.

        Args:
            session_id:  Unique identifier for this developer's session.
            voice_text:  Transcribed voice input.

        Returns:
            Dict always containing:
                - voice_response       (str)
                - actions_proposed     (list)
                - requires_confirmation (bool)
        """
        try:
            memory: list[dict] = list(self._sessions.get(session_id, []))
            current_mode: str = self._session_modes.get(session_id, "dev")

            # Classify (Phase 5: pass current_mode for fast-path detection)
            classification = self.classify_intent(voice_text, memory, current_mode)
            logger.info(
                "[%s] Intent: %s (confidence: %.2f) mode_switch=%r",
                session_id, classification["intent"], classification["confidence"],
                classification.get("mode_switch"),
            )

            # Apply mode switch if indicated
            mode_switch = classification.get("mode_switch")
            if mode_switch and "to" in mode_switch:
                self._session_modes[session_id] = mode_switch["to"]
                logger.info(
                    "[%s] Mode switched: %s → %s",
                    session_id, mode_switch.get("from"), mode_switch["to"],
                )

            # Short-circuit for clarify route
            if classification.get("route_to") == "clarify":
                result: dict = {
                    "voice_response": classification["clarifying_question"]
                        or "Could you clarify what you need?",
                    "actions_proposed": [],
                    "requires_confirmation": False,
                    "mode_switch": None,
                    "intent": "ambiguous",
                }
            else:
                result = await self.dispatch_to_agents(
                    classification["intent"],
                    {
                        "voice_text": voice_text,
                        "classification": classification,
                        "session_memory": memory,
                    },
                )
                # Propagate mode_switch into the result for server.py to emit
                if mode_switch and "mode_switch" not in result:
                    result["mode_switch"] = mode_switch

            # Always surface the classified intent so audio_stream can emit mode_change
            result["intent"] = classification["intent"]

            # Append turn to memory and trim to rolling 10-entry window
            memory.append({"role": "user",  "content": voice_text})
            memory.append({"role": "vega",  "content": result["voice_response"]})
            self._sessions[session_id] = memory[-10:]

            return result

        except OrchestratorError as exc:
            logger.error("[%s] OrchestratorError in process_turn: %s", session_id, exc)
            return dict(_SAFE_FALLBACK)
        except Exception as exc:
            logger.exception("[%s] Unexpected error in process_turn: %s", session_id, exc)
            return dict(_SAFE_FALLBACK)

    def get_session_memory(self, session_id: str) -> list[dict]:
        """
        Return a copy of the session memory for the given session.

        Args:
            session_id: Session identifier.

        Returns:
            List of turn dicts, or an empty list if the session does not exist.
        """
        return list(self._sessions.get(session_id, []))

    def get_session_mode(self, session_id: str) -> str:
        """
        Return the current mode ("dev" | "ops") for a session.

        Args:
            session_id: Session identifier.

        Returns:
            Current mode string, defaulting to "dev" if never set.
        """
        return self._session_modes.get(session_id, "dev")

    def set_session_mode(self, session_id: str, mode: str) -> None:
        """
        Explicitly set the mode for a session (called by POST /session/start).

        Args:
            session_id: Session identifier.
            mode:       "dev" or "ops".
        """
        if mode not in ("dev", "ops"):
            raise ValueError(f"Invalid mode {mode!r} — must be 'dev' or 'ops'")
        self._session_modes[session_id] = mode

    def clear_session(self, session_id: str) -> None:
        """
        Remove all memory for a session.

        Args:
            session_id: Session identifier. No-op if the session does not exist.
        """
        if session_id in self._sessions:
            del self._sessions[session_id]
            self._session_modes.pop(session_id, None)
            logger.debug("Cleared session memory for %r", session_id)


# ---------------------------------------------------------------------------
# Smoke test (dev only — not pytest)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    async def _smoke_test():
        print("=" * 60)
        print("orchestrator.py smoke test")
        print("=" * 60)

        try:
            agent = OrchestratorAgent()
        except FileNotFoundError as exc:
            print(f"\n[FATAL] {exc}", file=sys.stderr)
            sys.exit(1)
        except OrchestratorError as exc:
            print(f"\n[FATAL] OrchestratorError during init: {exc}", file=sys.stderr)
            sys.exit(1)

        # Turn 1
        print("\n[1] process_turn — Lambda 500 errors")
        result1 = await agent.process_turn(
            "test_session",
            "My Lambda auth function is returning 500 errors",
        )
        print(f"    voice_response:        {result1['voice_response']}")
        print(f"    actions_proposed:      {result1['actions_proposed']}")
        print(f"    requires_confirmation: {result1['requires_confirmation']}")

        # Turn 2
        print("\n[2] process_turn — security audit")
        result2 = await agent.process_turn(
            "test_session",
            "Review my authentication module for security vulnerabilities",
        )
        print(f"    voice_response:        {result2['voice_response']}")
        print(f"    actions_proposed:      {result2['actions_proposed']}")
        print(f"    requires_confirmation: {result2['requires_confirmation']}")

        print(f"\n    Session memory entries after 2 turns: {len(agent.get_session_memory('test_session'))}")

        print("\norchestrator.py smoke test passed")

    asyncio.run(_smoke_test())
