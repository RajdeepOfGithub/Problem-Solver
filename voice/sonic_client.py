"""
voice/sonic_client.py
Nova 2 Sonic client — real-time bidirectional speech via Amazon Bedrock.

Migrated to AWS Strands Agents BidiAgent framework (strands-agents>=1.23).
The Strands layer owns session lifecycle, reconnects, and event routing.
Direct aws_sdk_bedrock_runtime calls are removed from this module.

Nova Sonic uses a SINGLE persistent bidirectional stream per WebSocket session:
  - Input:  raw 16kHz PCM from the user  → STT transcript
  - Output: 16kHz LPCM audio from Vega   → TTS audio chunks

Architecture note: this module never touches FastAPI or WebSocket directly.
audio_stream.py owns the WebSocket; this module owns the Bedrock session.

Interface contract with audio_stream.py:
  - VegaBidiSession(...) — persistent session, one per WebSocket connection
  - AudioChunkEvent, TranscriptEvent, TranscriptEventType data classes
  - check_nova_sonic_connectivity() coroutine → dict

Import note: import from submodules to avoid strands.experimental.bidi.__init__
which requires pyaudio (not installed). The submodule imports below bypass it.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

import boto3
from dotenv import load_dotenv

# ── pyaudio stub ───────────────────────────────────────────────────────────
# strands.experimental.bidi.__init__ imports BidiAudioIO which does a top-level
# "import pyaudio" for microphone/speaker I/O.  We never use BidiAudioIO —
# our audio goes over WebSocket, not a sound card — but the import still runs.
# Inject a minimal stub so the module-level import succeeds without the
# PortAudio C library being installed.  Any actual pyaudio call would still
# raise AttributeError, which would surface as a clear bug rather than a
# silent no-op.
import sys as _sys
if "pyaudio" not in _sys.modules:
    import types as _types
    _pyaudio_stub = _types.ModuleType("pyaudio")
    # Attributes referenced at class-definition time in strands.experimental.bidi.io.audio
    # (used by BidiAudioIO which we never instantiate — these are type annotation stubs only)
    _pyaudio_stub.PyAudio = type("PyAudio", (), {})     # type annotation: _audio: pyaudio.PyAudio
    _pyaudio_stub.Stream  = type("Stream",  (), {})     # type annotation: _stream: pyaudio.Stream
    _pyaudio_stub.paInt16 = 8                           # format constant
    _pyaudio_stub.paContinue = 0                        # stream callback return value
    _pyaudio_stub.get_sample_size = lambda fmt: 2       # bytes per sample for paInt16
    _sys.modules["pyaudio"] = _pyaudio_stub

# Direct submodule imports — strands.experimental.bidi.* (pyaudio stub above must
# already be in sys.modules before this line executes)
from strands.experimental.bidi.agent.agent import BidiAgent
from strands.experimental.bidi.models.nova_sonic import BidiNovaSonicModel
from strands.experimental.bidi.types.events import (
    BidiAudioInputEvent,
    BidiAudioStreamEvent,
    BidiConnectionCloseEvent,
    BidiErrorEvent,
    BidiResponseCompleteEvent,
    BidiTranscriptStreamEvent,
)

load_dotenv()
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

NOVA_SONIC_MODEL_ID: str = os.getenv("NOVA_SONIC_MODEL_ID", "amazon.nova-2-sonic-v1:0")
AWS_REGION:          str = os.getenv("AWS_REGION", "us-east-1")

AUDIO_INPUT_SAMPLE_RATE:  int = 16000   # User PCM input (matches WebSocket client)
AUDIO_OUTPUT_SAMPLE_RATE: int = 16000   # Strands normalizes Nova Sonic output to 16kHz
AUDIO_CHANNELS:           int = 1

# Default system prompt injected into every Nova Sonic session.
_DEFAULT_SYSTEM_PROMPT: str = (
    "You are Vega, a voice-powered AI staff engineer. "
    "You help developers with code review, security audits, and incident investigation. "
    "Be direct, technical, and concise. Maximum 3 sentences per response."
)


# ─────────────────────────────────────────────
# Data classes — MUST NOT change (audio_stream.py depends on these)
# ─────────────────────────────────────────────

class TranscriptEventType(str, Enum):
    PARTIAL = "partial"   # Intermediate — may change
    FINAL   = "final"     # Committed — ready for agent pipeline


@dataclass
class TranscriptEvent:
    """Emitted by VegaBidiSession for each transcription chunk received."""
    text:       str
    event_type: TranscriptEventType
    confidence: Optional[float] = None

    @property
    def is_final(self) -> bool:
        return self.event_type == TranscriptEventType.FINAL


@dataclass
class AudioChunkEvent:
    """Emitted for each audio chunk synthesized by Nova Sonic."""
    audio_bytes:       bytes
    is_final:          bool
    highlighted_nodes: list[str] = None   # Diagram sync — Codebase Explorer Agent only

    def to_base64(self) -> str:
        return base64.b64encode(self.audio_bytes).decode("utf-8")

    def __post_init__(self):
        if self.highlighted_nodes is None:
            self.highlighted_nodes = []


# ─────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────

async def _maybe_await(value) -> None:
    """Await a value if it is a coroutine; otherwise discard it."""
    if asyncio.iscoroutine(value):
        await value


# ─────────────────────────────────────────────
# VegaBidiSession — persistent Nova Sonic session via Strands BidiAgent
# ─────────────────────────────────────────────

class VegaBidiSession:
    """
    Wraps Strands BidiAgent for a single persistent WebSocket session.

    One VegaBidiSession is created when the WebSocket connects and lives
    until it disconnects — no per-utterance teardown/restart. The Strands
    BidiAgent handles session lifecycle, reconnects, and event routing.

    Multi-turn works automatically: BidiNovaSonicModel._send_audio_content()
    re-opens the audio content block whenever a new chunk arrives after
    _end_audio_input() closed the previous one.

    Interface for audio_stream.py:

        session = VegaBidiSession(
            session_id      = "sess_abc",
            system_prompt   = "You are Vega...",
            on_transcript   = async_fn(text: str, is_final: bool),
            on_audio_output = async_fn(audio_bytes: bytes, is_final: bool),
        )
        await session.start()
        await session.send_audio(pcm_bytes)         # stream audio chunks
        await session.signal_end_of_utterance()     # on empty frame from client
        await session.stop()                        # on WebSocket disconnect
    """

    def __init__(
        self,
        session_id:      Optional[str]      = None,
        system_prompt:   Optional[str]      = None,
        on_transcript:   Optional[Callable] = None,
        on_audio_output: Optional[Callable] = None,
    ) -> None:
        self._session_id      = session_id or str(uuid.uuid4())
        self._system_prompt   = system_prompt or _DEFAULT_SYSTEM_PROMPT
        self._on_transcript   = on_transcript
        self._on_audio_output = on_audio_output

        # Build BidiNovaSonicModel with Nova Sonic v2 config.
        # turn_detection=HIGH lets Nova Sonic auto-detect end-of-utterance;
        # signal_end_of_utterance() provides explicit control on top of this.
        self._model = BidiNovaSonicModel(
            model_id=NOVA_SONIC_MODEL_ID,
            provider_config={
                "audio": {
                    "input_rate":  AUDIO_INPUT_SAMPLE_RATE,
                    "output_rate": AUDIO_OUTPUT_SAMPLE_RATE,
                    "channels":    AUDIO_CHANNELS,
                    "format":      "pcm",
                    "voice":       "matthew",
                },
                "inference": {
                    "max_tokens":  1024,
                    "temperature": 0.7,
                    "top_p":       0.9,
                },
                "turn_detection": {
                    "endpointingSensitivity": "HIGH",
                },
            },
            client_config={
                "region": AWS_REGION,
            },
        )

        # BidiAgent wraps the model and owns the event loop / tool execution
        self._agent = BidiAgent(
            model=self._model,
            system_prompt=self._system_prompt,
        )

        self._event_task: Optional[asyncio.Task] = None
        self._started: bool = False

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Start the BidiAgent session and launch the background event loop.

        Call once after WebSocket connects. Blocks until the Bedrock stream
        is open and the initialization event sequence is complete.
        """
        await self._agent.start(invocation_state={"session_id": self._session_id})
        self._event_task = asyncio.create_task(self._event_loop())
        self._started = True
        logger.info("[%s] VegaBidiSession started — model=%s", self._session_id, NOVA_SONIC_MODEL_ID)

    async def stop(self) -> None:
        """
        Stop the session and release all resources.
        Call when the WebSocket disconnects.
        """
        self._started = False
        if self._event_task and not self._event_task.done():
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
        try:
            await self._agent.stop()
        except Exception as exc:
            logger.warning("[%s] Agent stop error (ignored): %s", self._session_id, exc)
        logger.info("[%s] VegaBidiSession stopped", self._session_id)

    # ── Audio input ───────────────────────────────────────────────────

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """
        Send a raw PCM audio chunk to Nova Sonic.

        Args:
            pcm_bytes: Raw 16kHz mono 16-bit PCM bytes from the WebSocket client.

        BidiNovaSonicModel._send_audio_content() auto-opens a new audio content
        block if none is active — enabling seamless multi-turn without restart.
        """
        if not pcm_bytes or not self._started:
            return
        b64 = base64.b64encode(pcm_bytes).decode("utf-8")
        await self._agent.send(
            BidiAudioInputEvent(
                audio=b64,
                format="pcm",
                sample_rate=AUDIO_INPUT_SAMPLE_RATE,
                channels=AUDIO_CHANNELS,
            )
        )

    async def signal_end_of_utterance(self) -> None:
        """
        Explicitly signal end-of-utterance to Nova Sonic.

        Closes the current audio content block so Nova Sonic finalises the
        transcript and generates a response. The next send_audio() call will
        automatically open a fresh audio content block for the next turn.

        With turn_detection=HIGH the model may already have detected the
        pause; this call is a belt-and-suspenders safety gate tied to the
        empty binary frame in the WebSocket API spec.
        """
        if not self._started:
            return
        try:
            await self._model._end_audio_input()
            logger.debug("[%s] End-of-utterance signaled to Nova Sonic", self._session_id)
        except Exception as exc:
            logger.warning("[%s] signal_end_of_utterance error: %s", self._session_id, exc)

    # ── Background event loop ─────────────────────────────────────────

    async def _event_loop(self) -> None:
        """
        Background task: consume BidiAgent output events and route to callbacks.

        Event routing:
          BidiAudioStreamEvent      → on_audio_output(bytes, is_final=False)
          BidiTranscriptStreamEvent (role=user)
                                    → on_transcript(text, is_final)
          BidiResponseCompleteEvent → on_audio_output(b"", is_final=True)
          BidiConnectionCloseEvent  → log + break
          BidiErrorEvent            → log error
        """
        try:
            async for event in self._agent.receive():

                # ── TTS audio from Nova Sonic ──────────────────────────────
                if isinstance(event, BidiAudioStreamEvent):
                    audio_bytes = base64.b64decode(event.audio)
                    if self._on_audio_output:
                        await _maybe_await(self._on_audio_output(audio_bytes, False))

                # ── STT transcript ─────────────────────────────────────────
                elif isinstance(event, BidiTranscriptStreamEvent):
                    if event.role == "user" and self._on_transcript:
                        await _maybe_await(self._on_transcript(event.text, event.is_final))

                # ── Response complete (TTS stream done) ────────────────────
                elif isinstance(event, BidiResponseCompleteEvent):
                    if self._on_audio_output:
                        await _maybe_await(self._on_audio_output(b"", True))
                    logger.debug(
                        "[%s] Response complete: stop_reason=%s",
                        self._session_id, event.stop_reason,
                    )

                # ── Error ──────────────────────────────────────────────────
                elif isinstance(event, BidiErrorEvent):
                    logger.error(
                        "[%s] Nova Sonic error [%s]: %s",
                        self._session_id, event.code, event.message,
                    )

                # ── Connection closed ──────────────────────────────────────
                elif isinstance(event, BidiConnectionCloseEvent):
                    logger.info(
                        "[%s] Nova Sonic connection closed: reason=%s",
                        self._session_id, event.reason,
                    )
                    break

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[%s] Event loop error: %s", self._session_id, exc)
        finally:
            logger.debug("[%s] Event loop task done", self._session_id)


# ─────────────────────────────────────────────
# SonicTTSStream — stub (unchanged)
# ─────────────────────────────────────────────

class SonicTTSStream:
    """
    NOT IMPLEMENTED — TTS is handled natively by VegaBidiSession.

    Nova Sonic uses a single bidirectional stream for both STT (user audio in →
    transcript) and TTS (text in → Vega audio out). Vega's spoken response
    arrives as BidiAudioStreamEvent events on the same stream. Pass an
    on_audio_output callback to VegaBidiSession to receive TTS audio.
    """

    def __init__(self) -> None:
        pass

    async def synthesize(
        self,
        text: str,
        highlighted_nodes: Optional[list[str]] = None,
        voice_id: str = "matthew",
    ) -> AsyncGenerator[AudioChunkEvent, None]:
        raise NotImplementedError(
            "SonicTTSStream.synthesize() is not implemented. "
            "TTS is handled via BidiAudioStreamEvent in VegaBidiSession. "
            "Pass on_audio_output= to VegaBidiSession.__init__() to receive audio."
        )
        yield  # async generator marker

    async def synthesize_sentence_list(
        self,
        sentences: list[dict],
    ) -> AsyncGenerator[AudioChunkEvent, None]:
        raise NotImplementedError(
            "SonicTTSStream.synthesize_sentence_list() is not implemented. "
            "TTS is handled via BidiAudioStreamEvent in VegaBidiSession."
        )
        yield


# ─────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────

async def check_nova_sonic_connectivity() -> dict:
    """
    Lightweight connectivity check for Nova Sonic. Called by GET /health.

    Uses boto3's bedrock client (list_foundation_models) as a low-cost probe —
    the full bidirectional stream is not opened here.

    Returns:
        {"status": "connected" | "degraded" | "disconnected", "error": str | None}
    """
    try:
        bedrock = boto3.client(
            "bedrock",
            region_name=AWS_REGION,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )
        loop   = asyncio.get_event_loop()
        models = await loop.run_in_executor(
            None,
            lambda: bedrock.list_foundation_models(
                byProvider="Amazon",
                byOutputModality="SPEECH",
            ),
        )
        model_ids = [m["modelId"] for m in models.get("modelSummaries", [])]

        if NOVA_SONIC_MODEL_ID in model_ids or any("sonic" in m.lower() for m in model_ids):
            return {"status": "connected", "model_id": NOVA_SONIC_MODEL_ID}

        return {
            "status": "degraded",
            "error":  f"Model {NOVA_SONIC_MODEL_ID} not found in available speech models",
        }

    except Exception as exc:
        return {"status": "disconnected", "error": str(exc)}
