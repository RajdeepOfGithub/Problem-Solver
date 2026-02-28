"""
voice/audio_stream.py
Real-time audio streaming pipeline — manages the per-connection audio state
between the WebSocket client and Nova Sonic.

This module sits between the FastAPI WebSocket handler (api/server.py) and
the Nova Sonic client (sonic_client.py). It owns:
  - One persistent VegaBidiSession per WebSocket connection (no per-utterance restart)
  - Utterance accumulation (collecting PCM chunks for duration tracking)
  - Transcript routing → agent pipeline dispatch
  - TTS audio forwarding from Nova Sonic → WebSocket client

One AudioStreamSession is created per WebSocket connection. The server calls
  await session.open()   after the WebSocket connects
  await session.close()  when the WebSocket disconnects
and sends audio/control frames in between.
"""

import asyncio
import base64
import logging
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from agents.orchestrator import OrchestratorAgent, OrchestratorError
from voice.sonic_client import (
    AudioChunkEvent,
    VegaBidiSession,
    TranscriptEvent,
    TranscriptEventType,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

# Pre-playback buffer: accumulate this many ms before starting audio playback
# Absorbs network jitter without causing voice stuttering (Architecture spec §8)
PLAYBACK_BUFFER_MS = 300
AUDIO_SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2  # 16-bit PCM
BUFFER_BYTES = int((PLAYBACK_BUFFER_MS / 1000) * AUDIO_SAMPLE_RATE * BYTES_PER_SAMPLE)

# Max silence duration before auto-triggering end-of-utterance (ms)
# Prevents sessions from hanging if the client forgets to send an empty frame
SILENCE_TIMEOUT_MS = 2000

# Latency budget: agent pipeline must complete within this window
AGENT_TIMEOUT_SECONDS = 10.0


# ─────────────────────────────────────────────
# Session state machine
# ─────────────────────────────────────────────


class SessionState(str, Enum):
    IDLE = "idle"                         # Waiting for audio input
    RECEIVING_AUDIO = "receiving_audio"   # Streaming audio from client
    TRANSCRIBING = "transcribing"         # STT in progress
    PROCESSING = "processing"             # Agent pipeline running
    SPEAKING = "speaking"                 # TTS streaming back to client
    AWAITING_CONFIRMATION = "awaiting_confirmation"  # Safety gate active


@dataclass
class UtteranceBuffer:
    """Accumulates raw PCM chunks for a single developer utterance."""
    chunks: list[bytes] = field(default_factory=list)
    start_time: float = field(default_factory=time.monotonic)
    last_chunk_time: float = field(default_factory=time.monotonic)

    def append(self, chunk: bytes):
        self.chunks.append(chunk)
        self.last_chunk_time = time.monotonic()

    def total_bytes(self) -> int:
        return sum(len(c) for c in self.chunks)

    def duration_ms(self) -> float:
        return (self.last_chunk_time - self.start_time) * 1000

    def is_silent_timeout(self) -> bool:
        elapsed_ms = (time.monotonic() - self.last_chunk_time) * 1000
        return elapsed_ms >= SILENCE_TIMEOUT_MS

    def clear(self):
        self.chunks.clear()
        self.start_time = time.monotonic()
        self.last_chunk_time = self.start_time


# ─────────────────────────────────────────────
# Callbacks — the server wires these in
# ─────────────────────────────────────────────


@dataclass
class StreamCallbacks:
    """
    Callback hooks the FastAPI server registers to receive stream events.
    Each callback is async and receives a dict matching the WS frame spec in API.md.
    """
    on_transcript: Callable       # {"type": "transcript", "text": ..., "is_final": bool}
    on_action_update: Callable    # {"type": "action_update", ...}
    on_response_audio: Callable   # {"type": "response_audio", "chunk": base64, ...}
    on_confirmation_required: Callable  # {"type": "confirmation_required", ...}
    on_error: Callable            # {"type": "error", "code": ..., "message": ...}
    # Phase 5: mode switch notification
    on_mode_switch: Optional[Callable] = None  # {"type": "mode_switch", "from": ..., "to": ...}


# ─────────────────────────────────────────────
# Core session — one per WebSocket connection
# ─────────────────────────────────────────────


class AudioStreamSession:
    """
    Manages the full audio lifecycle for a single WebSocket connection.

    The server calls:
      - await session.open()                — after WebSocket connects
      - session.receive_audio_chunk(bytes)  — for each binary frame from client
      - session.signal_end_of_utterance()   — for the empty frame from client
      - session.speak(text, nodes)           — to mark TTS state (audio comes from Nova Sonic)
      - session.interrupt()                  — when the client starts speaking mid-response
      - await session.close()               — when WebSocket disconnects

    The session emits events back via StreamCallbacks.

    A single VegaBidiSession (persistent Strands BidiAgent) is held for the
    lifetime of the WebSocket connection — no per-utterance teardown/restart.
    Multi-turn works because BidiNovaSonicModel auto-opens a new audio content
    block on each send_audio() call after end-of-utterance closes the previous one.
    """

    def __init__(
        self,
        session_id: str,
        callbacks: StreamCallbacks,
        agent_dispatcher: Optional[Callable] = None,
    ):
        self.session_id = session_id
        self.callbacks = callbacks
        self.agent_dispatcher = agent_dispatcher  # Wired in by server once orchestrator is ready

        self.state = SessionState.IDLE
        self._utterance_buffer = UtteranceBuffer()

        # Persistent Nova Sonic session — created in open(), torn down in close()
        self._bidi_session: Optional[VegaBidiSession] = None

        # TTS playback buffer — pre-queue audio before streaming to client
        self._playback_buffer: bytearray = bytearray()
        self._playback_buffer_flushed: bool = False

        # Active TTS task — cancelled on interrupt
        self._tts_task: Optional[asyncio.Task] = None

        # Silence watchdog
        self._silence_watchdog: Optional[asyncio.Task] = None

        # Orchestrator — one instance per session, owns session memory internally
        self.orchestrator = OrchestratorAgent()

        logger.info(f"AudioStreamSession created: {session_id}")

    # ─── Lifecycle ────────────────────────────────

    async def open(self) -> None:
        """
        Start the persistent Nova Sonic session.
        Must be called once after the WebSocket connection is established.
        """
        self._bidi_session = VegaBidiSession(
            session_id=self.session_id,
            on_transcript=self._handle_transcript,
            on_audio_output=self._handle_tts_audio,
        )
        await self._bidi_session.start()
        logger.info(f"[{self.session_id}] Bidi session open — ready for audio")

    # ─── Audio input ─────────────────────────────

    async def receive_audio_chunk(self, pcm_bytes: bytes):
        """
        Called by the WebSocket handler for each binary frame from the client.
        Empty bytes = end-of-utterance signal (matching the API spec).
        """
        if not pcm_bytes:
            # Empty frame = end of utterance (API spec: Client → Server)
            await self.signal_end_of_utterance()
            return

        # Interrupt any active TTS if the developer starts speaking again
        if self.state == SessionState.SPEAKING:
            await self.interrupt()

        self.state = SessionState.RECEIVING_AUDIO
        self._utterance_buffer.append(pcm_bytes)

        if self._bidi_session:
            await self._bidi_session.send_audio(pcm_bytes)

        # Reset silence watchdog
        await self._reset_silence_watchdog()

    async def signal_end_of_utterance(self):
        """
        Called when the client sends an empty binary frame.
        Signals Nova Sonic that the utterance is complete, triggers transcription
        finalization. The next send_audio() automatically starts the next turn.
        """
        logger.debug(
            f"[{self.session_id}] End of utterance — "
            f"{self._utterance_buffer.total_bytes()} bytes, "
            f"{self._utterance_buffer.duration_ms():.0f}ms"
        )

        if self._silence_watchdog:
            self._silence_watchdog.cancel()

        self._utterance_buffer.clear()

        if self._bidi_session:
            await self._bidi_session.signal_end_of_utterance()
            self.state = SessionState.TRANSCRIBING

    # ─── Transcript handler (called from VegaBidiSession._event_loop) ────

    async def _handle_transcript(self, text: str, is_final: bool) -> None:
        """
        Receive a transcript event from Nova Sonic and forward to the client.
        On final transcript, dispatch to the agent pipeline as a background task
        so the event loop in VegaBidiSession is never blocked.
        """
        await self.callbacks.on_transcript({
            "type":     "transcript",
            "text":     text,
            "is_final": is_final,
        })

        if is_final:
            logger.info(f"[{self.session_id}] Final transcript: '{text}'")
            self.state = SessionState.PROCESSING
            # Fire-and-forget — must not block the Nova Sonic event loop
            asyncio.create_task(self._dispatch_with_timeout(text))

    async def _dispatch_with_timeout(self, transcript: str) -> None:
        """Wrap _dispatch_to_agents with a hard 15s timeout guard."""
        try:
            await asyncio.wait_for(
                self._dispatch_to_agents(transcript),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            logger.error(f"[{self.session_id}] Agent dispatch timed out after 15s")
            self.state = SessionState.IDLE
        except Exception as exc:
            logger.error(f"[{self.session_id}] Agent dispatch error: {exc}")
            self.state = SessionState.IDLE

    # ─── TTS handler (called from VegaBidiSession._event_loop) ───────────

    async def _handle_tts_audio(self, audio_bytes: bytes, is_final: bool):
        """
        Receives Vega's TTS audio from Nova Sonic's BidiAudioStreamEvents and
        forwards it to the WebSocket client as response_audio frames.
        Called by VegaBidiSession via on_audio_output= callback.
        """
        if audio_bytes:
            await self.callbacks.on_response_audio({
                "type":             "response_audio",
                "chunk":            base64.b64encode(audio_bytes).decode(),
                "is_final":         is_final,
                "highlighted_nodes": [],
            })
        elif is_final:
            # Empty bytes + is_final = TTS stream complete; signal the client
            await self.callbacks.on_response_audio({
                "type":             "response_audio",
                "chunk":            "",
                "is_final":         True,
                "highlighted_nodes": [],
            })

        if is_final and self.state == SessionState.SPEAKING:
            self.state = SessionState.IDLE

    # ─── Agent dispatch ──────────────────────────

    async def _dispatch_to_agents(self, transcript: str):
        """
        Forward the final transcript to the Orchestrator agent pipeline.
        The dispatcher is injected by the server (wired after Phase 3 is running).

        If no dispatcher is wired yet, echoes back the transcript as a stub.
        """
        try:
            if self.agent_dispatcher:
                # Full agent pipeline (wired in Phase 4+)
                async with asyncio.timeout(AGENT_TIMEOUT_SECONDS):
                    result = await self.agent_dispatcher(
                        session_id=self.session_id,
                        transcript=transcript,
                    )
                    if result:
                        await self._handle_agent_result(result)
            else:
                # Phase 4+: Orchestrator pipeline — intent classification + agent dispatch
                async with asyncio.timeout(AGENT_TIMEOUT_SECONDS):
                    result = await asyncio.to_thread(
                        self.orchestrator.process_turn,
                        self.session_id,
                        transcript,
                    )

                voice_response = result.get("voice_response", "")
                actions_proposed = result.get("actions_proposed", [])
                requires_confirmation = result.get("requires_confirmation", False)

                # Phase 5: emit mode_switch frame if orchestrator triggered a mode change
                mode_switch = result.get("mode_switch")
                if mode_switch and isinstance(mode_switch, dict) and self.callbacks.on_mode_switch:
                    try:
                        await self.callbacks.on_mode_switch({
                            "type":   "mode_switch",
                            "from":   mode_switch.get("from", "dev"),
                            "to":     mode_switch.get("to", "ops"),
                            "reason": result.get("context_summary", ""),
                        })
                    except Exception as exc:
                        logger.error(f"[{self.session_id}] mode_switch callback error: {exc}")

                # Safety gate: send confirmation_required frame before TTS
                if requires_confirmation and actions_proposed:
                    action = actions_proposed[0]
                    await self.callbacks.on_confirmation_required({
                        "type":      "confirmation_required",
                        "action_id": action.get("action_id", ""),
                        "prompt":    action.get("description", "Should I proceed with this action?"),
                    })

                # Emit action_update frame for every proposed action
                for action in actions_proposed:
                    await self.callbacks.on_action_update({
                        "type":        "action_update",
                        "action_id":   action.get("action_id", ""),
                        "description": action.get("description", ""),
                        "status":      "pending",
                    })

                # Speak the agent voice response — skip TTS if response is empty
                if voice_response:
                    await self.speak(voice_response)
                else:
                    self.state = SessionState.IDLE

        except TimeoutError:
            logger.error(f"[{self.session_id}] Agent pipeline timeout after {AGENT_TIMEOUT_SECONDS}s")
            await self.callbacks.on_error({
                "type":    "error",
                "code":    "AGENT_TIMEOUT",
                "message": f"Agent pipeline exceeded {AGENT_TIMEOUT_SECONDS}s processing limit",
            })
            self.state = SessionState.IDLE
        except OrchestratorError as e:
            logger.error(f"Orchestrator error for session {self.session_id}: {e}")
            await self.callbacks.on_error({
                "type":    "error",
                "code":    "AGENT_TIMEOUT",
                "message": str(e),
            })
            self.state = SessionState.IDLE
        except Exception as e:
            logger.exception(f"Unexpected error in agent dispatch: {e}")
            await self.callbacks.on_error({
                "type":    "error",
                "code":    "AGENT_ERROR",
                "message": str(e),
            })
            self.state = SessionState.IDLE

    async def _handle_agent_result(self, result: dict):
        """
        Route agent results to the correct output channel.
        Called with the structured response from the Orchestrator.
        """
        result_type = result.get("type", "voice_response")

        if result_type == "confirmation_required":
            # Safety gate — send confirmation prompt, change state
            self.state = SessionState.AWAITING_CONFIRMATION
            await self.callbacks.on_confirmation_required({
                "type":      "confirmation_required",
                "action_id": result.get("action_id"),
                "prompt":    result.get("prompt"),
            })
            # Also speak the confirmation prompt aloud
            await self.speak(result.get("prompt", "Should I proceed?"))

        elif result_type == "action_update":
            await self.callbacks.on_action_update({
                "type":        "action_update",
                "action_id":   result.get("action_id"),
                "description": result.get("description"),
                "status":      result.get("status"),
            })

        elif result_type == "voice_response":
            # Standard text response — speak it
            text = result.get("text", "")
            if text:
                await self.speak(text)

        elif result_type == "walkthrough":
            # Codebase Explorer — sentence list with highlighted_nodes
            sentences = result.get("walkthrough", [])
            await self.speak_sentence_list(sentences)

    # ─── TTS output ──────────────────────────────

    async def speak(self, text: str, highlighted_nodes: Optional[list[str]] = None):
        """
        Mark the session as speaking. TTS audio is delivered asynchronously via
        _handle_tts_audio() as Nova Sonic emits BidiAudioStreamEvents on the
        persistent bidirectional session — no separate synthesize() call needed.
        """
        self.state = SessionState.SPEAKING
        self._playback_buffer.clear()
        self._playback_buffer_flushed = False

    async def speak_sentence_list(self, sentences: list[dict]):
        """
        Synthesize a codebase explorer walkthrough sentence list.
        TTS audio arrives via _handle_tts_audio() from Nova Sonic audio events.
        """
        self.state = SessionState.SPEAKING

    async def _emit_audio_chunk(self, chunk: AudioChunkEvent):
        """
        Emit an audio chunk to the client, respecting the 300ms pre-playback buffer.

        Buffer rule (Architecture spec §8):
          - Accumulate chunks until BUFFER_BYTES is reached
          - Once flushed, stream subsequent chunks immediately
          - is_final=True always flushes immediately
        """
        if chunk.is_final:
            # Final chunk: flush any remaining buffer and signal completion
            if self._playback_buffer and not self._playback_buffer_flushed:
                await self.callbacks.on_response_audio({
                    "type":  "response_audio",
                    "chunk": AudioChunkEvent(
                        audio_bytes=bytes(self._playback_buffer),
                        is_final=False,
                        highlighted_nodes=chunk.highlighted_nodes,
                    ).to_base64(),
                    "is_final":          False,
                    "highlighted_nodes": [],
                })
                self._playback_buffer.clear()

            await self.callbacks.on_response_audio({
                "type":             "response_audio",
                "chunk":            "",
                "is_final":         True,
                "highlighted_nodes": [],
            })
            return

        if self._playback_buffer_flushed:
            # Buffer already flushed — stream directly
            await self.callbacks.on_response_audio({
                "type":             "response_audio",
                "chunk":            chunk.to_base64(),
                "is_final":         False,
                "highlighted_nodes": chunk.highlighted_nodes,
            })
        else:
            # Still buffering
            self._playback_buffer.extend(chunk.audio_bytes)
            if len(self._playback_buffer) >= BUFFER_BYTES:
                # Buffer full — flush and mark as flushed
                self._playback_buffer_flushed = True
                await self.callbacks.on_response_audio({
                    "type":  "response_audio",
                    "chunk": AudioChunkEvent(
                        audio_bytes=bytes(self._playback_buffer),
                        is_final=False,
                        highlighted_nodes=chunk.highlighted_nodes,
                    ).to_base64(),
                    "is_final":          False,
                    "highlighted_nodes": [],
                })
                self._playback_buffer.clear()

    # ─── Interrupt ───────────────────────────────

    async def interrupt(self):
        """
        Interrupt active TTS when the developer starts speaking mid-response.
        Cancels the TTS task and flushes the playback buffer.
        """
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()
            try:
                await self._tts_task
            except asyncio.CancelledError:
                pass

        self._playback_buffer.clear()
        self._playback_buffer_flushed = False
        self.state = SessionState.IDLE
        logger.debug(f"[{self.session_id}] TTS interrupted by user")

    # ─── Silence watchdog ────────────────────────

    async def _reset_silence_watchdog(self):
        """Restart the silence watchdog timer on each audio chunk received."""
        if self._silence_watchdog:
            self._silence_watchdog.cancel()
        self._silence_watchdog = asyncio.create_task(self._silence_watchdog_task())

    async def _silence_watchdog_task(self):
        """
        Auto-trigger end-of-utterance after SILENCE_TIMEOUT_MS of no audio.
        Prevents sessions from hanging if the client loses connection mid-utterance.
        """
        await asyncio.sleep(SILENCE_TIMEOUT_MS / 1000)
        if self.state == SessionState.RECEIVING_AUDIO:
            logger.debug(
                f"[{self.session_id}] Silence timeout — auto-triggering end of utterance"
            )
            await self.signal_end_of_utterance()

    # ─── Cleanup ─────────────────────────────────

    async def close(self):
        """Release all resources when the WebSocket disconnects."""
        if self._silence_watchdog:
            self._silence_watchdog.cancel()
        if self._tts_task:
            self._tts_task.cancel()
        if self._bidi_session:
            await self._bidi_session.stop()
            self._bidi_session = None
        self.state = SessionState.IDLE
        logger.info(f"AudioStreamSession closed: {self.session_id}")
