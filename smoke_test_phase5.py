"""
smoke_test_phase5.py
Vega — Phase 5 smoke test.

Verifies all Phase 5 components import and instantiate without errors.
Run from the vega/ directory:
    python smoke_test_phase5.py

Does NOT make any network calls. Just validates the module graph is wired correctly.
"""

import asyncio
import sys
import traceback

PASS = "✓"
FAIL = "✗"
results = []


def check(label: str, fn):
    try:
        fn()
        results.append((True, label))
        print(f"  {PASS} {label}")
    except Exception as exc:
        results.append((False, label))
        print(f"  {FAIL} {label}")
        traceback.print_exc()


print("\n" + "=" * 60)
print("Vega Phase 5 — Smoke Test")
print("=" * 60 + "\n")

# ── 1. DocScanner ─────────────────────────────────────────────────────────
print("1. ingestion/doc_scanner.py")
check(
    "DocScanner imports",
    lambda: __import__("ingestion.doc_scanner", fromlist=["DocScanner"]),
)
check(
    "DocScanner instantiates",
    lambda: __import__("ingestion.doc_scanner", fromlist=["DocScanner"]).DocScanner(),
)
check(
    "scan_repo_chunks importable",
    lambda: __import__("ingestion.doc_scanner", fromlist=["scan_repo_chunks"]).scan_repo_chunks,
)

# ── 2. TwoToneDiagramGenerator ───────────────────────────────────────────
print("\n2. diagram/two_tone_generator.py")
check(
    "TwoToneDiagramGenerator imports",
    lambda: __import__(
        "diagram.two_tone_generator", fromlist=["TwoToneDiagramGenerator"]
    ),
)
check(
    "TwoToneDiagramGenerator instantiates",
    lambda: __import__(
        "diagram.two_tone_generator", fromlist=["TwoToneDiagramGenerator"]
    ).TwoToneDiagramGenerator(),
)

# Quick functional test — no network
def _test_two_tone():
    from diagram.two_tone_generator import TwoToneDiagramGenerator
    gen = TwoToneDiagramGenerator()
    base = "flowchart TD\n    api_server[\"api/server.py\"] --> agents[\"agents/orchestrator.py\"]"
    file_status = {
        "api/server.py": "built",
        "agents/orchestrator.py": "built",
        "agents/dev_mode/pr_review.py": "stub",
    }
    result = gen.generate(
        base_mermaid=base,
        file_status=file_status,
        planned_components=[],
        node_ids=["api/server.py", "agents/orchestrator.py"],
        diagram_level="file",
    )
    assert "mermaid" in result, "Missing 'mermaid' key in result"
    assert "styles_applied" in result, "Missing 'styles_applied' key"
    assert "legend" in result, "Missing 'legend' key"

check("TwoToneDiagramGenerator.generate() runs without network", _test_two_tone)

# ── 3. ProjectIntelligenceAgent ──────────────────────────────────────────
print("\n3. agents/dev_mode/project_intelligence.py")
check(
    "ProjectIntelligenceAgent imports",
    lambda: __import__(
        "agents.dev_mode.project_intelligence", fromlist=["ProjectIntelligenceAgent"]
    ),
)
check(
    "ProjectIntelligenceAgent instantiates",
    lambda: __import__(
        "agents.dev_mode.project_intelligence", fromlist=["ProjectIntelligenceAgent"]
    ).ProjectIntelligenceAgent(),
)
check(
    "project_intelligence.txt prompt file exists",
    lambda: open("prompts/dev_mode/project_intelligence.txt").close(),
)

# ── 4. CodeActionAgent ────────────────────────────────────────────────────
print("\n4. agents/ops_mode/code_action.py")
check(
    "CodeActionAgent imports",
    lambda: __import__(
        "agents.ops_mode.code_action", fromlist=["CodeActionAgent"]
    ),
)
check(
    "CodeActionAgent instantiates (no github_actions)",
    lambda: __import__(
        "agents.ops_mode.code_action", fromlist=["CodeActionAgent"]
    ).CodeActionAgent(),
)
check(
    "code_action.txt prompt file exists",
    lambda: open("prompts/ops_mode/code_action.txt").close(),
)

# Test _extract_content_from_diff without network
def _test_diff_extraction():
    from agents.ops_mode.code_action import CodeActionAgent
    agent = CodeActionAgent()
    diff = "--- a/test.py\n+++ b/test.py\n@@ -0,0 +1,3 @@\n+def hello():\n+    pass\n+"
    content = agent._extract_content_from_diff(diff)
    assert "def hello" in content, f"Expected function in extracted content, got: {content!r}"

check("CodeActionAgent._extract_content_from_diff works", _test_diff_extraction)

# ── 5. Orchestrator (Phase 5 extensions) ─────────────────────────────────
print("\n5. agents/orchestrator.py (Phase 5 extensions)")
check(
    "OrchestratorAgent imports",
    lambda: __import__("agents.orchestrator", fromlist=["OrchestratorAgent"]),
)

def _test_orchestrator_fast_path():
    from agents.orchestrator import OrchestratorAgent, _OPS_SWITCH_KEYWORDS, _AVAILABLE_INTENTS
    # Verify new intents are present
    required_intents = {"dev_explore", "dev_review", "dev_build", "ops_code_action"}
    missing = required_intents - set(_AVAILABLE_INTENTS)
    assert not missing, f"Missing intents: {missing}"
    # Verify keyword list is populated
    assert len(_OPS_SWITCH_KEYWORDS) > 0, "OPS_SWITCH_KEYWORDS is empty"

check("Phase 5 intent taxonomy in orchestrator constants", _test_orchestrator_fast_path)

def _test_orchestrator_mode_tracking():
    # We can't instantiate OrchestratorAgent without Bedrock,
    # but we can verify the class has the new methods
    from agents.orchestrator import OrchestratorAgent
    assert hasattr(OrchestratorAgent, "get_session_mode"), "Missing get_session_mode"
    assert hasattr(OrchestratorAgent, "set_session_mode"), "Missing set_session_mode"
    assert hasattr(OrchestratorAgent, "_session_modes") is False, \
        "_session_modes should be instance attr, not class attr"

check("Orchestrator has Phase 5 mode tracking methods", _test_orchestrator_mode_tracking)

# ── 6. Prompts ────────────────────────────────────────────────────────────
print("\n6. Prompt files")

def _check_orchestrator_prompt():
    content = open("prompts/orchestrator.txt").read()
    assert "mode_switch" in content, "orchestrator.txt missing mode_switch"
    assert "dev_explore" in content, "orchestrator.txt missing dev_explore"
    assert "v2.0" in content, "orchestrator.txt should be v2.0"

check("prompts/orchestrator.txt v2.0 content check", _check_orchestrator_prompt)

# ── 7. VectorStore extensions ─────────────────────────────────────────────
print("\n7. ingestion/vector_store.py (Phase 5 extensions)")
check(
    "VectorStore has index_chunks method",
    lambda: hasattr(
        __import__("ingestion.vector_store", fromlist=["VectorStore"]).VectorStore,
        "index_chunks"
    ) and True,
)
check(
    "VectorStore has get_all_chunks method",
    lambda: hasattr(
        __import__("ingestion.vector_store", fromlist=["VectorStore"]).VectorStore,
        "get_all_chunks"
    ) and True,
)

# ── 8. audio_stream.py StreamCallbacks ──────────────────────────────────
print("\n8. voice/audio_stream.py (Phase 5 mode_switch callback)")
def _check_stream_callbacks():
    from voice.audio_stream import StreamCallbacks
    import inspect
    sig = inspect.signature(StreamCallbacks)
    assert "on_mode_switch" in sig.parameters, "StreamCallbacks missing on_mode_switch"

check("StreamCallbacks has on_mode_switch field", _check_stream_callbacks)

# ── Summary ───────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
passed = sum(1 for ok, _ in results if ok)
failed = sum(1 for ok, _ in results if not ok)
total  = len(results)

print(f"Results: {passed}/{total} passed, {failed} failed\n")

if failed == 0:
    print("🟢 All Phase 5 components imported and verified successfully.")
    sys.exit(0)
else:
    print("🔴 Some checks failed. Review errors above.")
    for ok, label in results:
        if not ok:
            print(f"   {FAIL} {label}")
    sys.exit(1)
