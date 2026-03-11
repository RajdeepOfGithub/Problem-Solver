"""
agents/ops_mode/log_parser.py
Vega — Log Parsing Agent

Receives raw AWS CloudWatch log data and extracts error events, exception
stack traces, and anomalous patterns relevant to the active incident.
Returns a structured log summary for the Root Cause Agent.

Model:  Amazon Nova 2 Lite via Bedrock converse API
Prompt: prompts/ops_mode/log_parser.txt
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
    Path(__file__).parent.parent.parent / "prompts" / "ops_mode" / "log_parser.txt"
)


class LogParserAgent:
    """
    Parses raw CloudWatch logs and extracts key events and anomaly patterns.

    Usage::

        agent = LogParserAgent()
        result = await agent.analyze(
            raw_logs=[...],
            incident={...},
            time_window_start="2026-03-04T10:00:00Z",
            time_window_end="2026-03-04T11:00:00Z",
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
        raw_logs: List[Dict],
        incident: Dict[str, Any],
        time_window_start: str = "",
        time_window_end: str = "",
    ) -> Dict[str, Any]:
        """
        Parse raw CloudWatch logs and extract signal from noise.

        Args:
            raw_logs:           List of CloudWatch log event dicts.
            incident:           Structured incident object from IncidentAnalysisAgent.
            time_window_start:  ISO 8601 start of log window.
            time_window_end:    ISO 8601 end of log window.

        Returns:
            Dict matching log_parser.txt output schema:
            {status, error_count, warning_count, key_events, anomaly_patterns, voice_summary}
        """
        if not raw_logs:
            logger.info("LogParserAgent: no logs provided — returning no_logs_found")
            return {
                "status": "no_logs_found",
                "error_count": 0,
                "warning_count": 0,
                "key_events": [],
                "anomaly_patterns": [],
                "voice_summary": "No logs were found in the specified time window.",
            }

        prompt = self._build_prompt(raw_logs, incident, time_window_start, time_window_end)

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
            logger.error("LogParserAgent: Bedrock error (%s): %s", code, exc)
            return self._error_response(str(exc))
        except json.JSONDecodeError as exc:
            logger.error("LogParserAgent: JSON parse error: %s", exc)
            return self._error_response(f"Model returned invalid JSON: {exc}")
        except Exception as exc:
            logger.error("LogParserAgent: unexpected error: %s", exc)
            return self._error_response(str(exc))

        logger.info(
            "LogParserAgent: parsed — errors=%d warnings=%d key_events=%d",
            result.get("error_count", 0),
            result.get("warning_count", 0),
            len(result.get("key_events", [])),
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        raw_logs: List[Dict],
        incident: Dict[str, Any],
        time_window_start: str,
        time_window_end: str,
    ) -> str:
        parts: List[str] = [
            f"INCIDENT: {json.dumps(incident, indent=2)[:2000]}",
            f"\nTIME WINDOW: {time_window_start} to {time_window_end}",
            f"\nRAW LOGS ({len(raw_logs)} events):",
        ]

        for log_event in raw_logs[:100]:
            ts = log_event.get("timestamp", "")
            msg = str(log_event.get("message", ""))[:500]
            parts.append(f"  [{ts}] {msg}")

        return "\n".join(parts)

    def _load_system_prompt(self) -> str:
        try:
            return _PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("log_parser.txt not found — using inline default")
            return (
                "You are Vega's Log Parsing Agent. Parse CloudWatch logs and extract key events. "
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
            "error_count": 0,
            "warning_count": 0,
            "key_events": [],
            "anomaly_patterns": [],
            "voice_summary": f"Log parsing could not complete: {message}",
        }
