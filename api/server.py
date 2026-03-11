"""
api/server.py
Vega FastAPI backend — Phase 5: Project Intelligence Layer.

Implements every endpoint from API.md plus Phase 5 additions:
  WS  /ws/voice                           — real-time bidirectional voice stream
  POST /repo/index                        — trigger GitHub repo ingestion
  GET  /repo/status/{job_id}              — poll indexing job progress
  GET  /repo/diagram/{job_id}             — retrieve generated Mermaid diagram
  GET  /repo/diagram/{job_id}/two-tone    — two-tone diagram (built/stub/planned)
  POST /session/start                     — create a new Dev/Ops session
  POST /session/optimize                  — trigger Project Intelligence analysis
  GET  /session/{session_id}/history      — retrieve conversation history
  GET  /session/{session_id}/actions      — retrieve action log
  POST /action/confirm                    — safety gate for destructive actions
  GET  /health                            — server + dependency health check

Architecture rule: WebSocket for voice (latency < 1.5s), REST for everything else.
Safety gate: no destructive action executes without a confirmed action_id.
Phase 5: mode_switch WebSocket frame emitted on Dev→Ops transitions.
"""

import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import boto3
from dotenv import load_dotenv
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from voice.audio_stream import AudioStreamSession, StreamCallbacks
from voice.sonic_client import check_nova_sonic_connectivity



load_dotenv()
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

API_SECRET_KEY = os.getenv("API_SECRET_KEY", "")
REQUIRE_CONFIRMATION = os.getenv("REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_ACTIONS", "true").lower() == "true"
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))
SERVER_START_TIME = time.monotonic()
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")


# ─────────────────────────────────────────────
# In-memory stores (Phase 3 — no DB yet)
# Will be extracted to a proper store layer in Phase 4+
# ─────────────────────────────────────────────

_indexing_jobs: dict[str, dict] = {}      # job_id → job state
_sessions: dict[str, dict] = {}           # session_id → session state
_actions: dict[str, dict] = {}            # action_id → action state
_active_ws_sessions: dict[str, AudioStreamSession] = {}  # session_id → active WS


# ─────────────────────────────────────────────
# App lifecycle
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown hooks."""
    logger.info("Vega server starting up...")
    # Future: warm up FAISS index, verify Bedrock connectivity
    yield
    logger.info("Vega server shutting down...")
    # Clean up any active WebSocket sessions
    for session in _active_ws_sessions.values():
        await session.close()


app = FastAPI(
    title="Vega API",
    description="Voice-powered AI Staff Engineer — Amazon Nova Hackathon 2026",
    version="0.5.0",  # Phase 5
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount frontend static files — must come after all route definitions
# in the source but is registered at import time. Path "/" is a catch-all
# so FastAPI routes take priority, static files serve what's left.
if os.path.isdir("frontend"):
    app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")


# ─────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────

def _generate_token(session_id: str) -> str:
    """Generate a cryptographically secure random session token."""
    return secrets.token_urlsafe(32)


def _validate_bearer_token(authorization: str) -> Optional[str]:
    """
    Validate the Bearer token from the Authorization header.
    Returns the session_id if valid, None if invalid.

    Phase 3 implementation: checks token against active sessions.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.replace("Bearer ", "").strip()
    # Find session with matching token
    for session_id, session in _sessions.items():
        if session.get("token") == token:
            return session_id
    return None


def _require_auth(authorization: str = Header(None, alias="Authorization")) -> str:
    """FastAPI dependency — validates bearer token and returns session_id."""
    session_id = _validate_bearer_token(authorization or "")
    if not session_id:
        raise HTTPException(status_code=401, detail="Invalid or expired bearer token")
    return session_id


# ─────────────────────────────────────────────
# Request/Response models
# ─────────────────────────────────────────────

class RepoIndexRequest(BaseModel):
    repo_url: str
    branch: str = "main"
    github_token: str = ""

    @field_validator("repo_url")
    @classmethod
    def validate_repo_url(cls, v):
        if not v.startswith("https://github.com/"):
            raise ValueError("Only github.com repositories are supported")
        return v


class SessionStartRequest(BaseModel):
    mode: str
    repo_id: str

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v):
        if v not in ("dev", "ops"):
            raise ValueError("mode must be 'dev' or 'ops'")
        return v


class ActionConfirmRequest(BaseModel):
    session_id: str
    action_id: str
    confirmed: bool


class OptimizeRequest(BaseModel):
    session_id: str
    repo_id: str


# ─────────────────────────────────────────────
# WebSocket — WS /ws/voice
# ─────────────────────────────────────────────

@app.websocket("/ws/voice")
async def websocket_voice(websocket: WebSocket):
    """
    Real-time bidirectional voice stream endpoint.

    Protocol (from API.md):
      - Client sends raw 16kHz PCM mono binary chunks
      - Client sends empty binary frame to signal end-of-utterance
      - Server sends JSON frames: transcript, action_update, response_audio,
        confirmation_required, error

    Auth: Bearer token in Authorization header at handshake time.
    A session must be created via POST /session/start before connecting.
    """
    # Validate auth at handshake.
    # ⚠️ SECURITY NOTE: Browser WebSocket API cannot send custom headers.
    # The ?token= query param fallback is used by the dev test console at
    # frontend/test_voice.html. Query-param tokens are visible in server logs,
    # browser history, and proxy logs. In production, implement a short-lived
    # one-time ticket exchange (POST /session/ws-ticket) instead.
    auth_header = websocket.headers.get("Authorization", "") or \
        f"Bearer {websocket.query_params.get('token', '')}"
    session_id = _validate_bearer_token(auth_header)

    if not session_id:
        await websocket.close(code=4401, reason="AUTH_FAILED")
        logger.warning("WebSocket rejected: invalid bearer token")
        return

    if session_id not in _sessions:
        await websocket.close(code=4404, reason="SESSION_NOT_FOUND")
        logger.warning(f"WebSocket rejected: session {session_id} not found")
        return

    await websocket.accept()
    logger.info(f"WebSocket connected: session {session_id}")

    # ── Wire callbacks ────────────────────────────────────────────────────────
    # Each callback sends the appropriate JSON frame to the client

    async def on_transcript(frame: dict):
        try:
            await websocket.send_json(frame)
        except Exception as e:
            logger.error(f"on_transcript send error: {e}")

    async def on_action_update(frame: dict):
        try:
            await websocket.send_json(frame)
            # Persist action update to in-memory store
            action_id = frame.get("action_id")
            if action_id and action_id in _actions:
                _actions[action_id]["status"] = frame.get("status")
        except Exception as e:
            logger.error(f"on_action_update send error: {e}")

    async def on_response_audio(frame: dict):
        try:
            await websocket.send_json(frame)
        except Exception as e:
            logger.error(f"on_response_audio send error: {e}")

    async def on_confirmation_required(frame: dict):
        try:
            await websocket.send_json(frame)
            # Register the pending action in our action store
            action_id = frame.get("action_id")
            if action_id:
                _actions[action_id] = {
                    "action_id": action_id,
                    "session_id": session_id,
                    "status": "awaiting_confirmation",
                    "prompt": frame.get("prompt"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
        except Exception as e:
            logger.error(f"on_confirmation_required send error: {e}")

    async def on_error(frame: dict):
        try:
            await websocket.send_json(frame)
        except Exception as e:
            logger.error(f"on_error send error: {e}")

    async def on_mode_switch(frame: dict):
        try:
            # Add server timestamp before forwarding
            frame["timestamp"] = datetime.now(timezone.utc).isoformat() + "Z"
            await websocket.send_json(frame)
            logger.info(
                f"mode_switch emitted: {frame.get('from')} → {frame.get('to')} "
                f"(session {session_id})"
            )
        except Exception as e:
            logger.error(f"on_mode_switch send error: {e}")

    async def on_mode_change(frame: dict):
        try:
            await websocket.send_json(frame)
            logger.debug(
                f"mode_change emitted: intent={frame.get('intent')} "
                f"family={frame.get('mode_family')} (session {session_id})"
            )
        except Exception as e:
            logger.error(f"on_mode_change send error: {e}")

    callbacks = StreamCallbacks(
        on_transcript=on_transcript,
        on_action_update=on_action_update,
        on_response_audio=on_response_audio,
        on_confirmation_required=on_confirmation_required,
        on_error=on_error,
        on_mode_switch=on_mode_switch,
        on_mode_change=on_mode_change,
    )

    # ── Create audio session ──────────────────────────────────────────────────
    audio_session = AudioStreamSession(
        session_id=session_id,
        callbacks=callbacks,
        agent_dispatcher=None,  # Wired in Phase 4 when orchestrator is ready
    )
    _active_ws_sessions[session_id] = audio_session
    await audio_session.open()

    # ── Main receive loop ─────────────────────────────────────────────────────
    try:
        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            if message["type"] == "websocket.receive":
                if "bytes" in message and message["bytes"] is not None:
                    # Binary frame: raw PCM audio or empty end-of-utterance signal
                    await audio_session.receive_audio_chunk(message["bytes"])

                elif "text" in message and message["text"]:
                    # JSON control message (e.g., confirmation response from client)
                    try:
                        control = json.loads(message["text"])
                        await _handle_control_message(control, session_id, audio_session)
                    except json.JSONDecodeError:
                        pass

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: session {session_id}")
    except Exception as e:
        logger.error(f"WebSocket error (session {session_id}): {e}")
    finally:
        await audio_session.close()
        _active_ws_sessions.pop(session_id, None)
        logger.info(f"WebSocket session cleaned up: {session_id}")


async def _handle_control_message(
    message: dict,
    session_id: str,
    audio_session: AudioStreamSession,
):
    """Handle JSON control messages from the client (non-audio frames)."""
    msg_type = message.get("type")
    if msg_type == "interrupt":
        await audio_session.interrupt()


# ─────────────────────────────────────────────
# Repo indexing endpoints
# ─────────────────────────────────────────────

@app.post("/repo/index")
async def start_repo_index(
    request: RepoIndexRequest,
    background_tasks: BackgroundTasks,
    session_id: str = Depends(_require_auth),
):
    """
    Trigger GitHub repo ingestion — clone, chunk, embed, store in FAISS.
    Returns a job_id to poll via GET /repo/status/{job_id}.
    Requires authentication.
    """

    job_id = f"idx_{uuid.uuid4().hex[:8]}"
    _indexing_jobs[job_id] = {
        "job_id": job_id,
        "status": "indexing",
        "progress": 0,
        "chunks_indexed": 0,
        "total_chunks": 0,
        "file_count": 0,
        "repo_url": request.repo_url,
        "branch": request.branch,
        "error": None,
        "mermaid": None,
        "node_ids": [],
        "diagram_level": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Run ingestion in background
    background_tasks.add_task(
        _run_indexing_job,
        job_id=job_id,
        repo_url=request.repo_url,
        branch=request.branch,
        github_token=request.github_token,
    )

    return {
        "job_id": job_id,
        "status": "indexing",
        "file_count": 0,
        "estimated_duration_seconds": 45,
    }


async def _run_indexing_job(
    job_id: str,
    repo_url: str,
    branch: str,
    github_token: str,
):
    """
    Background task: runs the Phase 2 ingestion pipeline.
    Updates _indexing_jobs[job_id] throughout.
    """
    try:
        # Import Phase 2 ingestion components
        from ingestion.repo_loader import load_repo_to_dir
        from ingestion.embeddings import embed_chunks
        from ingestion.vector_store import VectorStore

        # Step 1: Clone and chunk — use load_repo_to_dir to persist for DocScanner
        repo_dir = os.path.join("data", "repos", job_id)
        chunks, repo_path, import_graph = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: load_repo_to_dir(
                repo_url,
                target_dir=repo_dir,
                branch=branch,
                github_token=github_token,
            ),
        )

        file_tree = list({c["file"] for c in chunks})
        file_count = len(file_tree)
        _indexing_jobs[job_id].update({
            "file_count": file_count,
            "total_chunks": len(chunks),
            "file_tree": file_tree,
            "repo_path": repo_path,
            "progress": 20,
        })

        # Enforce 100 file scope limit
        if file_count > 100:
            _indexing_jobs[job_id].update({
                "status": "failed",
                "error": f"Repository exceeds 100 file limit ({file_count} files). "
                         "Paste a link to a specific subdirectory instead.",
            })
            return

        # Step 2: Generate embeddings
        embedded_chunks = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: embed_chunks(chunks),
        )
        _indexing_jobs[job_id]["progress"] = 60

        # Step 3: Store in FAISS
        vector_store = VectorStore()
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: vector_store.index_chunks(embedded_chunks, index_id=job_id),
        )
        _indexing_jobs[job_id].update({
            "chunks_indexed": len(chunks),
            "progress": 80,
        })

        # Step 4: Generate diagram — walk disk, not FAISS index
        diagram_result = await _generate_repo_diagram(
            job_id=job_id,
            repo_local_path=repo_path,
            import_graph=import_graph,
        )
        _indexing_jobs[job_id].update(diagram_result)
        _indexing_jobs[job_id].update({
            "status": "complete",
            "progress": 100,
        })
        logger.info(f"Indexing complete: {job_id} ({file_count} files, {len(chunks)} chunks)")

    except Exception as e:
        logger.error(f"Indexing job {job_id} failed: {e}")
        _indexing_jobs[job_id].update({
            "status": "failed",
            "error": str(e),
        })


async def _generate_repo_diagram(
    job_id: str,
    repo_local_path: str,
    import_graph: dict,
) -> dict:
    """
    Generate nodes/edges graph by walking the cloned repo directory on disk.
    Uses ALL files (including binary/notebook/model files), not just FAISS-indexed ones.
    FAISS is for semantic search; the diagram needs the full file tree.
    """
    import re

    _SKIP_DIRS = {
        ".git", "__pycache__", "node_modules", ".ipynb_checkpoints",
        ".venv", "venv", "env", "dist", "build", ".idea", ".vscode",
    }
    _MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB — skip model checkpoints, etc.

    # Walk disk — source of truth for the diagram
    all_files: list[str] = []
    for root, dirs, files in os.walk(repo_local_path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for filename in files:
            if filename.startswith("."):
                continue
            filepath = os.path.join(root, filename)
            try:
                if os.path.getsize(filepath) > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            rel_path = os.path.relpath(filepath, repo_local_path).replace(os.sep, "/")
            all_files.append(rel_path)

    file_count = len(all_files)
    diagram_level = "file" if file_count <= 30 else "folder"

    def _sanitize(s: str) -> str:
        return re.sub(r"[^A-Za-z0-9]", "_", s).strip("_") or "node"

    def _file_type_info(path: str) -> tuple[str, str]:
        """Returns (file_type, accent_color) for a file path."""
        name = path.rsplit("/", 1)[-1].lower()
        if name.endswith(".ipynb"):
            return "notebook", "#0369a1"
        if any(name.endswith(ext) for ext in (".h5", ".pkl", ".pt", ".pth", ".onnx")):
            return "model", "#065f46"
        if any(name.endswith(ext) for ext in (".json", ".csv", ".parquet", ".tsv", ".npy")):
            return "data", "#92400e"
        if (any(name.endswith(ext) for ext in (".txt", ".md", ".rst", ".toml", ".yaml", ".yml", ".cfg"))
                or "requirements" in name or name in ("makefile", "dockerfile")):
            return "config", "#1e293b"
        if name.endswith(".py"):
            return "python", "#4338ca"
        if any(name.endswith(ext) for ext in (".js", ".ts", ".jsx", ".tsx")):
            return "javascript", "#b45309"
        return "file", "#334155"

    # JS/TS import edges — parse relative imports from .js/.ts/.jsx/.tsx files
    _JS_EXTS = {".js", ".ts", ".jsx", ".tsx"}
    _JS_IMPORT_RE = re.compile(
        r'''(?:import\s+.*?\s+from\s+['"](\.[^'"]+)['"]'''
        r'''|import\s*\(\s*['"](\.[^'"]+)['"]\s*\)'''
        r'''|require\s*\(\s*['"](\.[^'"]+)['"]\s*\))''',
    )
    all_files_set = set(all_files)
    for src_path in all_files:
        ext = os.path.splitext(src_path)[1]
        if ext not in _JS_EXTS:
            continue
        abs_src = os.path.join(repo_local_path, src_path)
        try:
            with open(abs_src, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read(32_000)
        except OSError:
            continue
        src_dir = os.path.dirname(src_path)
        for m in _JS_IMPORT_RE.finditer(content):
            raw = m.group(1) or m.group(2) or m.group(3)
            resolved = os.path.normpath(os.path.join(src_dir, raw)).replace(os.sep, "/")
            matched = None
            if resolved in all_files_set:
                matched = resolved
            else:
                for try_ext in _JS_EXTS:
                    candidate = resolved + try_ext
                    if candidate in all_files_set:
                        matched = candidate
                        break
                if not matched and resolved + "/index.js" in all_files_set:
                    matched = resolved + "/index.js"
                if not matched and resolved + "/index.ts" in all_files_set:
                    matched = resolved + "/index.ts"
            if matched and matched != src_path:
                import_graph.setdefault(src_path, [])
                if matched not in import_graph[src_path]:
                    import_graph[src_path].append(matched)

    nodes: list[dict] = []
    edges: list[dict] = []
    node_ids: list[str] = []
    edge_counter = 0
    lines: list[str] = []

    if diagram_level == "file":
        # ── File-level: one node per file, folder --> file edges ───────────────
        lines = ["flowchart TD"]

        folders: set[str] = set()
        for path in all_files:
            parts = path.split("/")
            if len(parts) > 1:
                folders.add(parts[0])

        for folder in sorted(folders):
            nid = _sanitize(folder)
            lines.append(f'    {nid}["{folder}/"]')
            node_ids.append(nid)
            nodes.append({
                "id": folder + "/",
                "label": folder,
                "file_type": "folder",
                "node_type": "source",
                "accent_color": "#374151",
                "metadata": {"path": folder + "/", "imports": []},
            })

        for path in all_files:
            parts = path.split("/")
            file_nid = _sanitize(path)
            label = parts[-1]
            file_type, accent_color = _file_type_info(path)
            label_display = label.rsplit(".", 1)[0] if "." in label else label
            lines.append(f'    {file_nid}["{label}"]')
            node_ids.append(file_nid)
            nodes.append({
                "id": path,
                "label": label_display,
                "file_type": file_type,
                "node_type": "source",
                "accent_color": accent_color,
                "metadata": {"path": path, "imports": import_graph.get(path, [])},
            })
            if len(parts) > 1:
                folder_nid = _sanitize(parts[0])
                lines.append(f"    {folder_nid} --> {file_nid}")
                edge_counter += 1
                edges.append({
                    "id": f"e{edge_counter}",
                    "source": parts[0] + "/",
                    "target": path,
                    "label": "contains",
                })

        # Import edges (Python + JS/TS — import_graph includes both)
        seen_edges: set[tuple[str, str]] = set()
        for src, targets in list(import_graph.items())[:30]:
            src_nid = _sanitize(src)
            if src_nid not in node_ids:
                continue
            for tgt in targets[:5]:
                tgt_nid = _sanitize(tgt)
                if tgt_nid not in node_ids:
                    continue
                edge = (src_nid, tgt_nid)
                if edge in seen_edges or src_nid == tgt_nid:
                    continue
                seen_edges.add(edge)
                lines.append(f"    {src_nid} -.-> {tgt_nid}")
                edge_counter += 1
                edges.append({
                    "id": f"e{edge_counter}",
                    "source": src,
                    "target": tgt,
                    "label": "imports",
                })

    else:
        # ── Folder-level: one node per top-level folder ────────────────────────
        lines = ["flowchart TD"]

        folder_files: dict[str, list[str]] = {}
        for path in all_files:
            parts = path.split("/")
            top = parts[0] if len(parts) > 1 else "__root__"
            folder_files.setdefault(top, []).append(path)

        for folder, files in sorted(folder_files.items()):
            nid = _sanitize(folder)
            label = folder if folder != "__root__" else "(root)"
            count = len(files)
            lines.append(f'    {nid}["{label} ({count} files)"]')
            node_ids.append(nid)
            nodes.append({
                "id": folder,
                "label": label,
                "file_type": "folder",
                "node_type": "source",
                "accent_color": "#374151",
                "metadata": {"path": folder, "file_count": count, "imports": []},
            })

        def _top_folder(p: str) -> str:
            parts = p.split("/")
            return parts[0] if len(parts) > 1 else "__root__"

        seen_edges = set()
        for src, targets in list(import_graph.items())[:50]:
            src_folder = _top_folder(src)
            src_nid = _sanitize(src_folder)
            if src_nid not in node_ids:
                continue
            for tgt in targets[:5]:
                tgt_folder = _top_folder(tgt)
                tgt_nid = _sanitize(tgt_folder)
                if tgt_nid not in node_ids or tgt_folder == src_folder:
                    continue
                edge = (src_folder, tgt_folder)
                if edge in seen_edges:
                    continue
                seen_edges.add(edge)
                lines.append(f"    {src_folder} --> {tgt_folder}")
                edge_counter += 1
                edges.append({
                    "id": f"e{edge_counter}",
                    "source": src_folder,
                    "target": tgt_folder,
                    "label": "imports",
                })

    mermaid_text = "\n".join(lines)
    return {
        "mermaid": mermaid_text,
        "node_ids": node_ids,
        "nodes": nodes,
        "edges": edges,
        "diagram_level": diagram_level,
        "file_count": file_count,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _extract_mermaid_node_ids(mermaid_text: str) -> list[str]:
    """Extract node IDs from a Mermaid diagram string."""
    import re
    # Match patterns like: api_gateway["api/gateway.py"] or api_gateway[api/gateway.py]
    node_ids = []
    for match in re.finditer(r'\[["\'"]?([^"\'\]]+)["\'"]?\]', mermaid_text):
        label = match.group(1).strip()
        if "/" in label or "." in label:
            node_ids.append(label)
    return list(set(node_ids))


@app.get("/repo/diagram/{job_id}/two-tone")
async def get_two_tone_diagram(job_id: str):
    """
    Returns the two-tone Mermaid diagram with green/gray/dashed node classification.
    Requires indexing to be complete. DocScanner runs on demand using stored chunks.

    Response::

        {
            "job_id": str,
            "diagram_level": "file | folder",
            "mermaid": str,
            "node_ids": [...],
            "styles_applied": {file_path: "built"|"stub"|"planned"},
            "legend": str,
            "generated_at": str,
        }
    """
    if job_id not in _indexing_jobs:
        raise HTTPException(status_code=404, detail="job_id not found")

    job = _indexing_jobs[job_id]

    if job["status"] != "complete":
        raise HTTPException(status_code=404, detail="Indexing not yet complete")

    base_mermaid = job.get("mermaid", "")
    node_ids = job.get("node_ids", [])
    diagram_level = job.get("diagram_level", "file")
    repo_path = job.get("repo_path")  # Set by _run_indexing_job if load_repo_to_dir was used

    if not base_mermaid:
        raise HTTPException(status_code=500, detail="Base diagram not available")

    try:
        from ingestion.doc_scanner import DocScanner
        from diagram.two_tone_generator import TwoToneDiagramGenerator
        from ingestion.vector_store import VectorStore

        # Retrieve chunks to reconstruct md_contents
        vector_store = VectorStore()
        chunks = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: vector_store.get_all_chunks(index_id=job_id),
        )

        file_tree = list({c["file"] for c in chunks}) if chunks else []

        # Extract .md contents from chunks
        md_chunks: dict[str, list[str]] = {}
        for chunk in (chunks or []):
            if chunk.get("language") == "markdown":
                md_chunks.setdefault(chunk["file"], []).append(chunk["content"])
        md_contents = {p: "\n".join(parts) for p, parts in md_chunks.items()}

        # Run DocScanner
        scanner = DocScanner()
        scan_result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: scanner.scan_repo(
                file_tree=file_tree,
                md_contents=md_contents,
                repo_path=repo_path or ".",
            ),
        )

        # Generate two-tone diagram
        gen = TwoToneDiagramGenerator()
        two_tone = gen.generate(
            base_mermaid=base_mermaid,
            file_status=scan_result["file_status"],
            planned_components=scan_result["planned_components"],
            node_ids=node_ids,
            diagram_level=diagram_level,
        )

        return {
            "job_id": job_id,
            "diagram_level": diagram_level,
            "mermaid": two_tone["mermaid"],
            "node_ids": two_tone["node_ids"],
            "styles_applied": two_tone["styles_applied"],
            "legend": two_tone["legend"],
            "valid": two_tone["valid"],
            "generated_at": datetime.now(timezone.utc).isoformat() + "Z",
        }

    except Exception as e:
        logger.error(f"Two-tone diagram generation failed for {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Two-tone generation failed: {str(e)}")


@app.get("/repo/status/{job_id}")
async def get_repo_status(job_id: str):
    """Poll the progress of an ongoing indexing job."""
    if job_id not in _indexing_jobs:
        raise HTTPException(status_code=404, detail="job_id does not exist")
    job = _indexing_jobs[job_id]
    return {
        "job_id": job_id,
        "status": job["status"],
        "progress": job["progress"],
        "chunks_indexed": job["chunks_indexed"],
        "total_chunks": job["total_chunks"],
        "error": job.get("error"),
    }


@app.get("/repo/diagram/{job_id}")
async def get_repo_diagram(job_id: str):
    """Retrieve the generated SVG node/edge diagram for a completed indexing job."""
    if job_id not in _indexing_jobs:
        raise HTTPException(status_code=404, detail="job_id not found")

    job = _indexing_jobs[job_id]

    if job["status"] != "complete":
        raise HTTPException(status_code=404, detail="Indexing not yet complete")

    if not job.get("nodes"):
        raise HTTPException(status_code=500, detail="Diagram generation failed")

    return {
        "job_id": job_id,
        "diagram_level": job["diagram_level"],
        "file_count": job["file_count"],
        "nodes": job["nodes"],
        "edges": job["edges"],
        "generated_at": job["generated_at"],
    }


# ─────────────────────────────────────────────
# Session endpoints
# ─────────────────────────────────────────────

@app.post("/session/start")
async def start_session(request: SessionStartRequest):
    """
    Initialize a new Vega session. Must be called before opening a voice WebSocket.
    Returns a bearer token for WebSocket authentication.
    """
    # Validate that the repo index exists and is complete
    if request.repo_id == "test":
        logger.warning("Dev bypass: test session created without index validation")
    else:
        if request.repo_id not in _indexing_jobs:
            raise HTTPException(status_code=404, detail="repo_id index not found")

        if _indexing_jobs[request.repo_id]["status"] != "complete":
            raise HTTPException(status_code=404, detail="Repo index not yet complete")

    session_id = f"sess_{uuid.uuid4().hex[:8]}"
    token = _generate_token(session_id)
    now = datetime.now(timezone.utc).isoformat()

    _sessions[session_id] = {
        "session_id": session_id,
        "mode": request.mode,
        "repo_id": request.repo_id,
        "created_at": now,
        "last_active_at": now,
        "token": token,
        "messages": [],
        "actions": [],
        "message_count": 0,
        "action_count": 0,
    }

    logger.info(f"Session created: {session_id} ({request.mode} mode)")

    return {
        "session_id": session_id,
        "mode": request.mode,
        "repo_id": request.repo_id,
        "created_at": now,
        "token": token,  # Client uses this as Bearer token for WS auth
    }


@app.post("/session/optimize")
async def trigger_optimization(
    request: OptimizeRequest,
    session_id: str = Depends(_require_auth),
):
    """
    Trigger the Project Intelligence Agent for the current session.
    Runs DocScanner + ProjectIntelligenceAgent and returns optimization results.

    Called when the user requests gap analysis or optimization suggestions.
    Results include workflow_suggestions (Mermaid) and code_level_cards (max 5).

    Request: {"session_id": str, "repo_id": str}
    """
    if request.repo_id not in _indexing_jobs:
        raise HTTPException(status_code=404, detail="repo_id index not found")

    job = _indexing_jobs[request.repo_id]
    if job["status"] != "complete":
        raise HTTPException(status_code=422, detail="Repo index not yet complete")

    try:
        from ingestion.doc_scanner import DocScanner
        from agents.dev_mode.project_intelligence import ProjectIntelligenceAgent
        from ingestion.vector_store import VectorStore

        # Retrieve indexed chunks
        vector_store = VectorStore()
        chunks = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: vector_store.get_all_chunks(index_id=request.repo_id),
        )
        chunks = chunks or []

        file_tree = list({c["file"] for c in chunks})

        # Build md_contents from chunks
        md_chunks: dict[str, list[str]] = {}
        for chunk in chunks:
            if chunk.get("language") == "markdown":
                md_chunks.setdefault(chunk["file"], []).append(chunk["content"])
        md_contents = {p: "\n".join(parts) for p, parts in md_chunks.items()}

        # Build dependency_files from chunks
        dep_names = {"requirements.txt", "package.json", "Pipfile", "go.mod", "Cargo.toml"}
        dependency_files: dict[str, str] = {}
        for chunk in chunks:
            if chunk.get("file") in dep_names and chunk["file"] not in dependency_files:
                dependency_files[chunk["file"]] = chunk.get("content", "")

        # Run DocScanner
        scanner = DocScanner()
        repo_path = job.get("repo_path", ".")
        scan_result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: scanner.scan_repo(
                file_tree=file_tree,
                md_contents=md_contents,
                repo_path=repo_path,
            ),
        )

        # Run ProjectIntelligenceAgent
        agent = ProjectIntelligenceAgent()
        result = await agent.analyze(
            file_tree=file_tree,
            md_contents=md_contents,
            code_chunks=chunks[:20],
            planned_components=scan_result["planned_components"],
            file_status=scan_result["file_status"],
            dependency_files=dependency_files,
        )

        # Cache the result on the job for subsequent two-tone diagram calls
        _indexing_jobs[request.repo_id]["optimization_result"] = result

        return {
            "repo_id": request.repo_id,
            "session_id": request.session_id,
            **result,
        }

    except Exception as e:
        logger.error(f"Optimization failed for repo {request.repo_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Optimization failed: {str(e)}")


@app.get("/session/{session_id}/history")
async def get_session_history(
    session_id: str,
    auth_session_id: str = Depends(_require_auth),
):
    """Retrieve full conversation history for a session."""
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = _sessions[session_id]
    return {
        "session_id": session_id,
        "mode": session["mode"],
        "created_at": session["created_at"],
        "messages": session.get("messages", []),
    }


@app.get("/session/{session_id}/actions")
async def get_session_actions(
    session_id: str,
    auth_session_id: str = Depends(_require_auth),
):
    """Retrieve all autonomous actions taken or proposed in a session."""
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session_actions = [
        action for action in _actions.values()
        if action.get("session_id") == session_id
    ]

    return {"session_id": session_id, "actions": session_actions}


# ─────────────────────────────────────────────
# Safety gate — POST /action/confirm
# ─────────────────────────────────────────────

@app.post("/action/confirm")
async def confirm_action(
    request: ActionConfirmRequest,
    session_id: str = Depends(_require_auth),
):
    """
    Safety gate: confirm or reject a pending destructive action.

    This is a hard architectural requirement — no destructive action executes
    without a confirmed action_id (Architecture spec §7).

    Can be called directly for testing, or programmatically when voice
    confirmation is detected by the AudioStreamSession.
    """
    action_id = request.action_id

    if action_id not in _actions:
        raise HTTPException(status_code=404, detail="action_id not found in session")

    action = _actions[action_id]

    if action.get("session_id") != request.session_id:
        raise HTTPException(status_code=422, detail="action_id does not belong to session_id")

    if action["status"] not in ("awaiting_confirmation", "pending"):
        raise HTTPException(
            status_code=409,
            detail="Action already executed or cancelled — cannot re-confirm",
        )

    if not request.confirmed:
        _actions[action_id]["status"] = "cancelled"
        logger.info(f"Action cancelled: {action_id}")
        return {"action_id": action_id, "status": "cancelled"}

    # Mark as executing and hand off to action layer
    _actions[action_id].update({
        "status": "executing",
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
    })
    logger.info(f"Action confirmed and executing: {action_id}")

    # Dispatch to the appropriate action handler
    action_type = action.get("type", "")
    exec_result = None

    try:
        if action_type in ("create_file", "modify_file", "refactor"):
            from agents.ops_mode.code_action import CodeActionAgent
            agent = CodeActionAgent()
            action_result = action.get("code_action_result", {})
            if not action_result:
                action_result = {
                    "action_id": action_id,
                    "action_type": action_type,
                    "proposed_change": action.get("proposed_change", ""),
                    "target_file": action.get("target_file", ""),
                    "proposed_pr_title": action.get("proposed_pr_title", ""),
                    "proposed_pr_body": action.get("proposed_pr_body", ""),
                }
            exec_result = await agent.execute_action(action_result, confirmed=True)

        elif action_type in ("create_issue", "github_issue"):
            from actions import github_actions
            issue_data = action.get("issue_data", {})
            exec_result = github_actions.create_issue(
                owner=issue_data.get("owner", ""),
                repo=issue_data.get("repo", ""),
                title=issue_data.get("title", ""),
                body=issue_data.get("body", ""),
                labels=issue_data.get("labels"),
            )

        elif action_type in ("create_pr", "draft_pr"):
            from actions import github_actions
            pr_data = action.get("pr_data", {})
            exec_result = github_actions.create_draft_pr(
                owner=pr_data.get("owner", ""),
                repo=pr_data.get("repo", ""),
                title=pr_data.get("title", ""),
                body=pr_data.get("body", ""),
                head=pr_data.get("head", ""),
                base=pr_data.get("base", "main"),
            )

        else:
            logger.warning(f"Unknown action type: {action_type}")
            exec_result = {"status": "failed", "message": f"Unknown action type: {action_type}"}

        _actions[action_id]["status"] = "success"
        _actions[action_id]["result"] = exec_result
        logger.info(f"Action {action_id} executed successfully")

    except Exception as exc:
        logger.error(f"Action {action_id} execution failed: {exc}")
        _actions[action_id]["status"] = "failed"
        _actions[action_id]["error"] = str(exc)
        exec_result = {"status": "failed", "message": str(exc)}

    return {"action_id": action_id, "status": _actions[action_id]["status"], "result": exec_result}


# ─────────────────────────────────────────────
# Health check — GET /health
# ─────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """
    Server status + all external dependency connectivity.
    Use this to verify environment setup before a demo.
    """
    loop = asyncio.get_running_loop()
    uptime = int(time.monotonic() - SERVER_START_TIME)

    # Build reusable boto3 clients for this health check
    aws_region = os.getenv("AWS_REGION", "us-east-1")
    aws_bedrock_region = os.getenv("AWS_BEDROCK_REGION", aws_region)
    aws_key = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret = os.getenv("AWS_SECRET_ACCESS_KEY")

    def _make_client(service: str, region: str = aws_region):
        return boto3.client(
            service,
            region_name=region,
            aws_access_key_id=aws_key,
            aws_secret_access_key=aws_secret,
        )

    # Check Nova Sonic
    sonic_status = await check_nova_sonic_connectivity()

    # Check Bedrock (Nova Lite)
    bedrock_status = "disconnected"
    try:
        bedrock = _make_client("bedrock", aws_bedrock_region)
        await loop.run_in_executor(None, bedrock.list_foundation_models)
        bedrock_status = "connected"
    except Exception as e:
        logger.warning(f"Bedrock health check failed: {e}")
        bedrock_status = "disconnected"

    # Check GitHub
    github_status = "disconnected"
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"token {os.getenv('GITHUB_TOKEN', '')}"},
                timeout=5.0,
            )
            github_status = "connected" if resp.status_code == 200 else "degraded"
    except Exception:
        github_status = "disconnected"

    # Check AWS (CloudWatch)
    aws_status = "disconnected"
    try:
        logs_client = _make_client("logs")
        await loop.run_in_executor(
            None,
            lambda: logs_client.describe_log_groups(limit=1),
        )
        aws_status = "connected"
    except Exception:
        aws_status = "disconnected"

    # Check Lambda
    lambda_status = "disconnected"
    try:
        lam = _make_client("lambda")
        await loop.run_in_executor(
            None,
            lambda: lam.list_functions(MaxItems=1),
        )
        lambda_status = "connected"
    except Exception:
        lambda_status = "disconnected"

    # Check ECS
    ecs_status = "disconnected"
    try:
        ecs = _make_client("ecs")
        await loop.run_in_executor(None, ecs.list_clusters)
        ecs_status = "connected"
    except Exception:
        ecs_status = "disconnected"

    # Check FAISS index
    faiss_index_path = os.getenv("FAISS_INDEX_PATH", "./data/faiss_index")
    faiss_status = "loaded" if os.path.exists(faiss_index_path) else "not_initialized"

    overall = "ok" if all(
        s == "connected" for s in [bedrock_status, aws_status, github_status]
    ) else "degraded"

    return {
        "status": overall,
        "nova_sonic": sonic_status.get("status", "disconnected"),
        "bedrock": bedrock_status,
        "github": github_status,
        "cloudwatch": aws_status,
        "lambda": lambda_status,
        "ecs": ecs_status,
        "aws": aws_status,
        "faiss_index": faiss_status,
        "uptime_seconds": uptime,
        "active_sessions": len(_sessions),
        "active_ws_connections": len(_active_ws_sessions),
    }


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    uvicorn.run(
        "api.server:app",
        host=API_HOST,
        port=API_PORT,
        reload=True,
        reload_excludes=["data/*", "*.log"],
        log_level="info",
    )