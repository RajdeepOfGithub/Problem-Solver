"""
agents/dev_mode/project_intelligence.py
Vega — Phase 5: Project Intelligence Agent

Cross-references an indexed codebase against its documentation to produce
optimization suggestions in two passes:
  1. Internal gap analysis — planned components vs. actual file status
  2. External engineering intelligence — tech stack evaluation via Nova Lite

Returns a combined result matching the AGENTS.md Project Intelligence Agent schema.
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

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOVA_LITE_MODEL_ID: str = os.getenv("NOVA_LITE_MODEL_ID", "us.amazon.nova-2-lite-v1:0")
AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")

_PROMPT_PATH: Path = (
    Path(__file__).parent.parent.parent / "prompts" / "dev_mode" / "project_intelligence.txt"
)

_EFFORT_ORDER = {"low": 0, "medium": 1, "high": 2}

_SAFE_FALLBACK: Dict[str, Any] = {
    "status": "ok",
    "questions": [],
    "workflow_suggestions": {
        "has_changes": False,
        "mermaid": "",
        "changes_summary": "No structural changes recommended.",
    },
    "code_level_cards": [],
}


class ProjectIntelligenceAgent:
    """
    Cross-references codebase vs docs to produce optimization suggestions.

    Usage::

        agent = ProjectIntelligenceAgent()
        result = await agent.analyze(
            file_tree=["api/server.py", ...],
            md_contents={"README.md": "..."},
            code_chunks=[{"file": "api/server.py", "content": "..."}],
            planned_components=[{"name": "auth", "expected_path": "auth/middleware.py", ...}],
            file_status={"api/server.py": "built", "auth/middleware.py": "planned"},
            dependency_files={"requirements.txt": "fastapi\nboto3\n..."},
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def analyze(
        self,
        file_tree: List[str],
        md_contents: Dict[str, str],
        code_chunks: List[Dict],
        planned_components: List[Dict],
        file_status: Dict[str, str],
        dependency_files: Dict[str, str],
    ) -> Dict[str, Any]:
        """
        Main entry point. Returns optimization suggestions.

        Returns schema matching AGENTS.md Project Intelligence Agent output::

            {
                "status": "ok | needs_clarification",
                "questions": [],
                "workflow_suggestions": {
                    "has_changes": bool,
                    "mermaid": str,
                    "changes_summary": str,
                },
                "code_level_cards": [
                    {
                        "file": str,
                        "current_approach": str,
                        "suggested_approach": str,
                        "rationale": str,
                        "effort": "low | medium | high",
                    }
                ],
            }
        """
        try:
            # Run internal gap analysis (sync — no IO)
            internal_findings = self._analyze_internal_gaps(
                planned_components, file_status
            )

            # Run external engineering intelligence (async — Nova Lite call)
            external_suggestions = await self._analyze_external_intelligence(
                file_tree, code_chunks, dependency_files, md_contents
            )

            # Generate optimized workflow diagram
            workflow = await self._generate_optimized_diagram(
                file_tree, internal_findings, external_suggestions
            )

            # Merge, sort by effort, cap at 5
            cards = self._merge_and_prioritize(internal_findings, external_suggestions)

            return {
                "status": "ok",
                "questions": [],
                "workflow_suggestions": workflow,
                "code_level_cards": cards,
            }

        except Exception as exc:
            logger.error("ProjectIntelligenceAgent.analyze failed: %s", exc)
            return dict(_SAFE_FALLBACK)

    # ------------------------------------------------------------------
    # Internal — gap analysis
    # ------------------------------------------------------------------

    def _analyze_internal_gaps(
        self,
        planned_components: List[Dict],
        file_status: Dict[str, str],
    ) -> List[Dict]:
        """
        Compare planned vs built. Return list of gap findings (one per planned
        component that is not "built").
        """
        gaps: List[Dict] = []
        for comp in planned_components:
            expected_path = comp.get("expected_path", "")
            name = comp.get("name", expected_path)
            status = file_status.get(expected_path, "planned")
            mentioned_in = comp.get("mentioned_in", "documentation")
            signal = comp.get("signal", "")

            if status != "built":
                gaps.append({
                    "file": expected_path or name,
                    "current_approach": f"File is {status}",
                    "suggested_approach": (
                        f"Implement {name} as specified in {mentioned_in}"
                    ),
                    "rationale": (
                        f"Your roadmap mentions '{name}' in {mentioned_in} but the "
                        f"file is currently {status}."
                        + (f" Signal: \"{signal}\"" if signal else "")
                    ),
                    "effort": "medium",
                    "source": "internal",
                })
        return gaps

    # ------------------------------------------------------------------
    # Internal — external intelligence (Nova Lite)
    # ------------------------------------------------------------------

    async def _analyze_external_intelligence(
        self,
        file_tree: List[str],
        code_chunks: List[Dict],
        dependency_files: Dict[str, str],
        md_contents: Dict[str, str],
    ) -> List[Dict]:
        """
        Send project context to Nova Lite for external engineering analysis.
        Returns list of suggestion dicts, or [] on failure.
        """
        context = self._build_project_context(file_tree, dependency_files, md_contents)

        prompt = f"""You are a senior staff engineer reviewing a project's technical choices.

PROJECT CONTEXT:
{context}

TASK:
Analyze the project's technology choices, architecture patterns, and implementation approaches.
Identify specific improvements that would benefit this project based on its stated goals and current tech stack.

RULES:
- Every suggestion MUST reference a specific file, dependency, or pattern found in the project context above.
- Do NOT give generic advice. "Use caching" is not acceptable. "Your api/server.py handles /repo/index synchronously — for repos over 50 files, this will block the event loop. Add a background task queue using FastAPI BackgroundTasks" IS acceptable.
- Focus on: performance bottlenecks, missing error handling, security gaps, scalability concerns, dependency improvements.
- Maximum 5 suggestions, prioritized by effort (low effort = quick wins first).
- Each suggestion must include effort level: low (< 1 hour), medium (1-4 hours), high (> 4 hours).

OUTPUT FORMAT (valid JSON only, no markdown):
{{
    "suggestions": [
        {{
            "file": "specific/file/path.py or dependency name",
            "current_approach": "what the project currently does",
            "suggested_approach": "what should change and why",
            "rationale": "specific, evidence-based reasoning tied to this project",
            "effort": "low | medium | high"
        }}
    ]
}}"""

        try:
            response = await asyncio.to_thread(
                self._bedrock.converse,
                modelId=NOVA_LITE_MODEL_ID,
                system=[{"text": "You are a senior staff engineer. Return valid JSON only."}],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
            )
            raw: str = response["output"]["message"]["content"][0]["text"].strip()
            raw = self._strip_fences(raw)

            result = json.loads(raw)
            suggestions = result.get("suggestions", [])
            for s in suggestions:
                s["source"] = "external"
            return suggestions

        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            logger.warning("ProjectIntelligence: Bedrock error (%s): %s", code, exc)
            return []
        except json.JSONDecodeError as exc:
            logger.warning("ProjectIntelligence: JSON parse error in external analysis: %s", exc)
            return []
        except Exception as exc:
            logger.warning("ProjectIntelligence: external analysis failed: %s", exc)
            return []

    def _build_project_context(
        self,
        file_tree: List[str],
        dependency_files: Dict[str, str],
        md_contents: Dict[str, str],
    ) -> str:
        """Build a concise project context string for Nova Lite."""
        parts: List[str] = []

        parts.append("FILE TREE:")
        for f in file_tree[:50]:
            parts.append(f"  {f}")

        for dep_file, content in dependency_files.items():
            parts.append(f"\n{dep_file}:")
            parts.append(content[:2000])

        for md_path, content in md_contents.items():
            parts.append(f"\n{md_path} (excerpt):")
            parts.append(content[:500])

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Internal — optimized workflow diagram
    # ------------------------------------------------------------------

    async def _generate_optimized_diagram(
        self,
        file_tree: List[str],
        internal_findings: List[Dict],
        external_suggestions: List[Dict],
    ) -> Dict[str, Any]:
        """
        Generate an optimized workflow Mermaid diagram incorporating suggestions.
        Returns workflow_suggestions dict.
        """
        if not internal_findings and not external_suggestions:
            return {
                "has_changes": False,
                "mermaid": "",
                "changes_summary": "No structural changes recommended.",
            }

        changes: List[str] = []
        for f in internal_findings:
            changes.append(f"- Add: {f['file']} — {f['suggested_approach']}")
        for s in external_suggestions:
            if s.get("effort") == "low":
                changes.append(f"- Modify: {s['file']} — {s['suggested_approach']}")

        prompt = f"""Generate a Mermaid flowchart diagram showing a project's optimized workflow.

Current files (first 30):
{json.dumps(file_tree[:30])}

Recommended changes:
{chr(10).join(changes[:10])}

Generate a valid Mermaid flowchart (flowchart TD) showing the project structure with the recommended additions included. New nodes should be clearly labeled. Keep it under 20 nodes.

Return ONLY the Mermaid code. No markdown fences. No explanation."""

        try:
            response = await asyncio.to_thread(
                self._bedrock.converse,
                modelId=NOVA_LITE_MODEL_ID,
                system=[{"text": "Return valid Mermaid diagram code only. No markdown fences."}],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
            )
            mermaid: str = response["output"]["message"]["content"][0]["text"].strip()
            mermaid = self._strip_fences(mermaid)

            if not mermaid.startswith(("flowchart", "graph")):
                raise ValueError("Invalid Mermaid output from Nova Lite")

            n_internal = len(internal_findings)
            n_quick = len([s for s in external_suggestions if s.get("effort") == "low"])
            summary = (
                f"Recommended adding {n_internal} missing component{'s' if n_internal != 1 else ''} "
                f"and {n_quick} quick improvement{'s' if n_quick != 1 else ''}."
            )

            return {
                "has_changes": True,
                "mermaid": mermaid,
                "changes_summary": summary,
            }

        except Exception as exc:
            logger.warning("ProjectIntelligence: diagram generation failed: %s", exc)
            return {
                "has_changes": bool(internal_findings or external_suggestions),
                "mermaid": "",
                "changes_summary": (
                    f"Found {len(internal_findings)} gap(s) and "
                    f"{len(external_suggestions)} optimization(s)."
                ),
            }

    # ------------------------------------------------------------------
    # Internal — merge and prioritize
    # ------------------------------------------------------------------

    def _merge_and_prioritize(
        self, internal: List[Dict], external: List[Dict]
    ) -> List[Dict]:
        """
        Merge internal gap findings and external suggestions.
        Sort by effort (low first), cap at 5.
        """
        combined = internal + external
        combined.sort(key=lambda x: _EFFORT_ORDER.get(x.get("effort", "high"), 2))
        return combined[:5]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_fences(text: str) -> str:
        """Remove markdown code fences from Nova Lite output."""
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        return text.strip()

    def _load_system_prompt(self) -> str:
        """Load system prompt from file; fall back to a minimal default."""
        try:
            return _PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("project_intelligence.txt not found — using inline default")
            return (
                "You are Vega's Project Intelligence Agent. "
                "Analyze the codebase vs docs and return valid JSON only."
            )
