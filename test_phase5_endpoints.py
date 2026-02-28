#!/usr/bin/env python3
"""
Phase 5 Integration Test — Index Vega Repo & Hit All New Endpoints

Usage:
  1. Start the server:  uvicorn api.server:app --reload --port 8000
  2. Run this script:   python test_phase5_endpoints.py

Requires: GITHUB_TOKEN in .env (or pass as arg)
"""

import os
import sys
import json
import time
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "http://localhost:8000"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# ── CONFIG ──────────────────────────────────────────────────
# Point this to your Vega repo (or any repo under 100 files)
REPO_URL = os.getenv("VEGA_REPO_URL", "https://github.com/RajdeepOfGithub/Problem-Solver.git")
BRANCH = "main"
# ────────────────────────────────────────────────────────────

PASS = "✅"
FAIL = "❌"
WARN = "⚠️"
results = []


def log(label, passed, detail=""):
    status = PASS if passed else FAIL
    results.append(passed)
    print(f"  {status} {label}" + (f" — {detail}" if detail else ""))


def section(title):
    print(f"\n{'─'*60}\n  {title}\n{'─'*60}")


# ── 0. HEALTH CHECK ────────────────────────────────────────
section("0. Health Check")
try:
    r = requests.get(f"{BASE_URL}/health", timeout=5)
    health = r.json()
    log("Server reachable", r.status_code == 200, f"status={health.get('status')}")
    log("Bedrock connected", health.get("bedrock") == "connected")
    log("GitHub connected", health.get("github") == "connected")
except requests.ConnectionError:
    print(f"  {FAIL} Server not reachable at {BASE_URL}")
    print(f"\n  Start the server first:\n    uvicorn api.server:app --reload --port 8000\n")
    sys.exit(1)


# ── 1. INDEX REPO ──────────────────────────────────────────
section("1. Index Vega Repo")

if "YOUR_USER" in REPO_URL:
    print(f"  {WARN} Update REPO_URL in this script (or set VEGA_REPO_URL env var)")
    print(f"     Current value: {REPO_URL}")
    print(f"     Example: export VEGA_REPO_URL=https://github.com/yourname/vega")
    sys.exit(1)

index_payload = {
    "repo_url": REPO_URL,
    "branch": BRANCH,
    "github_token": GITHUB_TOKEN
}

r = requests.post(f"{BASE_URL}/repo/index", json=index_payload)
log("POST /repo/index accepted", r.status_code == 200, f"status_code={r.status_code}")

if r.status_code != 200:
    print(f"  Response: {r.text[:300]}")
    sys.exit(1)

index_data = r.json()
job_id = index_data.get("job_id", "")
log("Got job_id", bool(job_id), f"job_id={job_id}")
print(f"\n  Polling indexing status (max 120s)...")


# ── 2. POLL INDEXING STATUS ────────────────────────────────
section("2. Poll Indexing Status")

start = time.time()
status = "indexing"
while status == "indexing" and (time.time() - start) < 120:
    time.sleep(3)
    r = requests.get(f"{BASE_URL}/repo/status/{job_id}")
    status_data = r.json()
    status = status_data.get("status", "unknown")
    progress = status_data.get("progress", 0)
    chunks = status_data.get("chunks_indexed", 0)
    print(f"    ... {status} | progress={progress}% | chunks={chunks}", end="\r")

print()  # newline after \r
log("Indexing completed", status == "complete", f"final_status={status}")

if status != "complete":
    print(f"  Full response: {json.dumps(status_data, indent=2)}")
    print(f"\n  Indexing didn't complete. Check server logs for errors.")
    sys.exit(1)


# ── 3. STANDARD DIAGRAM ───────────────────────────────────
section("3. Standard Diagram")

r = requests.get(f"{BASE_URL}/repo/diagram/{job_id}")
log("GET /repo/diagram/{job_id}", r.status_code == 200, f"status_code={r.status_code}")

if r.status_code == 200:
    diagram_data = r.json()
    mermaid = diagram_data.get("mermaid", "")
    node_ids = diagram_data.get("node_ids", [])
    diagram_level = diagram_data.get("diagram_level", "unknown")
    log("Mermaid diagram returned", bool(mermaid), f"length={len(mermaid)} chars")
    log("Node IDs present", len(node_ids) > 0, f"count={len(node_ids)}")
    log("Diagram level set", diagram_level in ("file", "folder"), f"level={diagram_level}")
    
    # Show first 200 chars of diagram
    print(f"\n  Diagram preview:\n    {mermaid[:200]}...")
    print(f"\n  Node IDs (first 10): {node_ids[:10]}")


# ── 4. TWO-TONE DIAGRAM (Phase 5) ─────────────────────────
section("4. Two-Tone Diagram (Phase 5 NEW)")

r = requests.get(f"{BASE_URL}/repo/diagram/{job_id}/two-tone")
log("GET /repo/diagram/{job_id}/two-tone", r.status_code == 200, f"status_code={r.status_code}")

if r.status_code == 200:
    tt_data = r.json()
    tt_mermaid = tt_data.get("mermaid", "")
    styles = tt_data.get("styles_applied", {})
    
    log("Two-tone Mermaid returned", bool(tt_mermaid), f"length={len(tt_mermaid)} chars")
    log("Styles applied", len(styles) > 0, f"count={len(styles)}")
    
    # Count status types
    built = sum(1 for v in styles.values() if v == "built")
    stub = sum(1 for v in styles.values() if v == "stub")
    planned = sum(1 for v in styles.values() if v == "planned")
    log("File classification", built > 0, f"built={built} stub={stub} planned={planned}")
    
    # Check for green styles in mermaid
    has_green = "fill:#22c55e" in tt_mermaid
    has_gray = "fill:#6b7280" in tt_mermaid
    log("Green (built) styles present", has_green)
    log("Gray (stub/planned) styles present", has_gray or stub == 0, 
        "no stubs to style" if stub == 0 else "")
    
    # Show style breakdown
    print(f"\n  Style breakdown:")
    print(f"    🟢 Built:   {built}")
    print(f"    ⬜ Stub:    {stub}")
    print(f"    ▫️  Planned: {planned}")
    
    if styles:
        print(f"\n  First 5 file statuses:")
        for i, (path, st) in enumerate(list(styles.items())[:5]):
            icon = "🟢" if st == "built" else ("⬜" if st == "stub" else "▫️")
            print(f"    {icon} {path} → {st}")
else:
    print(f"  Response: {r.text[:300]}")


# ── 5. SESSION START ───────────────────────────────────────
section("5. Start Session")

session_payload = {"mode": "dev", "repo_id": job_id}
r = requests.post(f"{BASE_URL}/session/start", json=session_payload)
log("POST /session/start", r.status_code == 200, f"status_code={r.status_code}")

session_id = ""
if r.status_code == 200:
    session_data = r.json()
    session_id = session_data.get("session_id", "")
    log("Got session_id", bool(session_id), f"session_id={session_id}")
else:
    print(f"  Response: {r.text[:300]}")


# ── 6. OPTIMIZATION ENDPOINT (Phase 5) ────────────────────
section("6. Project Intelligence / Optimization (Phase 5 NEW)")

if session_id and job_id:
    optimize_payload = {"session_id": session_id, "repo_id": job_id}
    
    print(f"  Calling POST /session/optimize (this calls Nova Lite — may take 10-30s)...")
    r = requests.post(f"{BASE_URL}/session/optimize", json=optimize_payload, timeout=60)
    log("POST /session/optimize", r.status_code == 200, f"status_code={r.status_code}")
    
    if r.status_code == 200:
        opt_data = r.json()
        status = opt_data.get("status", "unknown")
        questions = opt_data.get("questions", [])
        workflow = opt_data.get("workflow_suggestions", {})
        cards = opt_data.get("code_level_cards", [])
        
        log("Status is ok", status == "ok", f"status={status}")
        log("Workflow suggestions returned", "has_changes" in workflow)
        log("Code-level cards returned", isinstance(cards, list), f"count={len(cards)}")
        log("Cards capped at 5", len(cards) <= 5, f"count={len(cards)}")
        
        if workflow.get("has_changes"):
            print(f"\n  Workflow summary: {workflow.get('changes_summary', 'N/A')}")
        
        if cards:
            print(f"\n  Code-level suggestion cards:")
            for i, card in enumerate(cards):
                effort_icon = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(card.get("effort", ""), "⚪")
                print(f"    {i+1}. {effort_icon} [{card.get('effort', '?')}] {card.get('file', '?')}")
                print(f"       Current:   {card.get('current_approach', 'N/A')[:80]}")
                print(f"       Suggested: {card.get('suggested_approach', 'N/A')[:80]}")
                print(f"       Rationale: {card.get('rationale', 'N/A')[:100]}")
                print()
        
        if questions:
            print(f"\n  Clarifying questions from agent:")
            for q in questions:
                print(f"    ❓ {q}")
    else:
        print(f"  Response: {r.text[:500]}")
else:
    print(f"  {WARN} Skipped — no valid session_id or job_id")


# ── 7. ORCHESTRATOR INTENT CLASSIFICATION ──────────────────
section("7. Orchestrator Intent Classification (via direct import)")

try:
    import asyncio
    from agents.orchestrator import Orchestrator
    
    orch = Orchestrator()
    
    test_cases = [
        ("create a new auth middleware file",   "ops_code_action", True),
        ("scan my code for security issues",    "dev_review",      False),
        ("walk me through the auth flow",       "dev_explore",     False),
        ("my Lambda is failing in production",  "ops_incident",    False),
        ("what should I build next",            "dev_build",       False),
    ]
    
    async def run_intent_tests():
        for text, expected_intent, expect_switch in test_cases:
            result = await orch.classify_intent(text, [])
            intent = result.get("intent", "unknown")
            has_switch = result.get("mode_switch") is not None
            
            intent_ok = intent == expected_intent
            switch_ok = has_switch == expect_switch
            
            icon = PASS if (intent_ok and switch_ok) else FAIL
            switch_str = f" + mode_switch" if has_switch else ""
            print(f"  {icon} \"{text[:45]}...\" → {intent}{switch_str}")
            
            if not intent_ok:
                print(f"      Expected: {expected_intent}, Got: {intent}")
            
            results.append(intent_ok and switch_ok)
    
    asyncio.run(run_intent_tests())

except ImportError as e:
    print(f"  {WARN} Could not import Orchestrator: {e}")
    print(f"      Run this script from the vega/ project root directory")
except Exception as e:
    print(f"  {FAIL} Orchestrator test failed: {e}")


# ── SUMMARY ────────────────────────────────────────────────
section("SUMMARY")

passed = sum(1 for r in results if r)
total = len(results)
all_pass = all(results)

print(f"\n  {'🟢' if all_pass else '🟡'} {passed}/{total} checks passed\n")

if not all_pass:
    print(f"  Failed checks — review the output above for details.")
    print(f"  Common issues:")
    print(f"    - Server not running → uvicorn api.server:app --reload --port 8000")
    print(f"    - GITHUB_TOKEN missing → check .env")
    print(f"    - Bedrock not connected → check AWS credentials")
    print(f"    - Repo URL wrong → set VEGA_REPO_URL env var")

sys.exit(0 if all_pass else 1)
