"""
test_phase3.py
Phase 3 smoke test — verifies all modules import cleanly and key classes
are structured correctly. Does NOT make live AWS/WebSocket calls.

Run with: python test_phase3.py
"""

import sys
import asyncio

def test_imports():
    """Test that all Phase 3 modules import without errors."""
    errors = []
    
    # Test sonic_client
    try:
        from voice.sonic_client import (
            SonicSTTStream,
            SonicTTSStream,
            TranscriptEvent,
            TranscriptEventType,
            AudioChunkEvent,
            check_nova_sonic_connectivity,
            NOVA_SONIC_MODEL_ID,
            AUDIO_SAMPLE_RATE,
        )
        print("  ✅ voice.sonic_client — all exports present")
    except ImportError as e:
        errors.append(f"voice.sonic_client import error: {e}")
        print(f"  ❌ voice.sonic_client — {e}")

    # Test audio_stream
    try:
        from voice.audio_stream import (
            AudioStreamSession,
            StreamCallbacks,
            SessionState,
            UtteranceBuffer,
            PLAYBACK_BUFFER_MS,
            AGENT_TIMEOUT_SECONDS,
        )
        print("  ✅ voice.audio_stream — all exports present")
    except ImportError as e:
        errors.append(f"voice.audio_stream import error: {e}")
        print(f"  ❌ voice.audio_stream — {e}")

    # Test server (FastAPI app)
    try:
        from api.server import app, _sessions, _actions, _indexing_jobs
        print(f"  ✅ api.server — FastAPI app created, version={app.version}")
    except ImportError as e:
        errors.append(f"api.server import error: {e}")
        print(f"  ❌ api.server — {e}")

    return errors


def test_data_classes():
    """Test that key data classes work correctly."""
    errors = []
    
    try:
        from voice.sonic_client import AudioChunkEvent, TranscriptEvent, TranscriptEventType
        import base64

        # AudioChunkEvent
        chunk = AudioChunkEvent(audio_bytes=b"\x00\x01\x02", is_final=False)
        assert chunk.highlighted_nodes == [], "highlighted_nodes should default to []"
        assert chunk.is_final == False
        b64 = chunk.to_base64()
        assert base64.b64decode(b64) == b"\x00\x01\x02"
        
        # TranscriptEvent
        event = TranscriptEvent(text="hello", event_type=TranscriptEventType.FINAL)
        assert event.is_final == True
        
        partial = TranscriptEvent(text="hel", event_type=TranscriptEventType.PARTIAL)
        assert partial.is_final == False

        print("  ✅ Data classes — AudioChunkEvent and TranscriptEvent work correctly")
    except Exception as e:
        errors.append(f"Data class test failed: {e}")
        print(f"  ❌ Data classes — {e}")

    return errors


def test_session_state_machine():
    """Test AudioStreamSession can be created with a stub dispatcher."""
    errors = []
    
    try:
        from voice.audio_stream import AudioStreamSession, StreamCallbacks, SessionState

        async def noop(frame): pass

        callbacks = StreamCallbacks(
            on_transcript=noop,
            on_action_update=noop,
            on_response_audio=noop,
            on_confirmation_required=noop,
            on_error=noop,
        )

        session = AudioStreamSession(
            session_id="test_sess_001",
            callbacks=callbacks,
            agent_dispatcher=None,
        )

        assert session.state == SessionState.IDLE, f"Expected IDLE, got {session.state}"
        assert session.session_id == "test_sess_001"
        
        print("  ✅ AudioStreamSession — creates correctly, initial state is IDLE")
    except Exception as e:
        errors.append(f"Session state machine test failed: {e}")
        print(f"  ❌ AudioStreamSession — {e}")

    return errors


def test_fastapi_routes():
    """Test that the FastAPI app has all routes from the API spec."""
    errors = []
    
    try:
        from api.server import app

        routes = {r.path: list(r.methods) for r in app.routes if hasattr(r, "methods") and r.methods}
        ws_routes = [r.path for r in app.routes if not hasattr(r, "methods") or not r.methods]

        expected_rest = [
            "/repo/index",
            "/repo/status/{job_id}",
            "/repo/diagram/{job_id}",
            "/session/start",
            "/session/{session_id}/history",
            "/session/{session_id}/actions",
            "/action/confirm",
            "/health",
        ]
        expected_ws = ["/ws/voice"]

        missing_rest = [r for r in expected_rest if r not in routes]
        missing_ws = [r for r in expected_ws if r not in ws_routes]

        if missing_rest:
            errors.append(f"Missing REST routes: {missing_rest}")
            print(f"  ❌ Missing REST routes: {missing_rest}")
        else:
            print(f"  ✅ REST routes — all {len(expected_rest)} routes registered")

        if missing_ws:
            errors.append(f"Missing WS routes: {missing_ws}")
            print(f"  ❌ Missing WS routes: {missing_ws}")
        else:
            print(f"  ✅ WebSocket routes — /ws/voice registered")

    except Exception as e:
        errors.append(f"FastAPI route test failed: {e}")
        print(f"  ❌ FastAPI routes — {e}")

    return errors


def test_utterance_buffer():
    """Test UtteranceBuffer accumulation logic."""
    errors = []
    
    try:
        from voice.audio_stream import UtteranceBuffer
        
        buf = UtteranceBuffer()
        assert buf.total_bytes() == 0
        
        buf.append(b"\x00" * 1000)
        buf.append(b"\x01" * 500)
        assert buf.total_bytes() == 1500
        
        buf.clear()
        assert buf.total_bytes() == 0
        
        print("  ✅ UtteranceBuffer — accumulation and clear work correctly")
    except Exception as e:
        errors.append(f"UtteranceBuffer test failed: {e}")
        print(f"  ❌ UtteranceBuffer — {e}")
    
    return errors


def main():
    print("=" * 55)
    print("  Vega Phase 3 — Smoke Test")
    print("=" * 55)

    all_errors = []

    print("\n[1/5] Module imports")
    all_errors.extend(test_imports())

    print("\n[2/5] Data classes")
    all_errors.extend(test_data_classes())

    print("\n[3/5] Session state machine")
    all_errors.extend(test_session_state_machine())

    print("\n[4/5] FastAPI routes")
    all_errors.extend(test_fastapi_routes())

    print("\n[5/5] UtteranceBuffer")
    all_errors.extend(test_utterance_buffer())

    print("\n" + "=" * 55)
    if all_errors:
        print(f"  ❌ FAILED — {len(all_errors)} error(s):")
        for e in all_errors:
            print(f"    • {e}")
        sys.exit(1)
    else:
        print("  ✅ ALL TESTS PASSED — Phase 3 structure is solid")
        print("  Ready to wire in Phase 2 ingestion and run the server.")
    print("=" * 55)


if __name__ == "__main__":
    main()
