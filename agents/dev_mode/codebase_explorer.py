# codebase_explorer.py
# Phase 5 — Codebase Explorer Agent
# Model:   Nova 2 Lite via Bedrock
# Prompt:  prompts/dev_mode/codebase_explorer.txt
#
# Role (per AGENTS.md):
#   Guides developers through unfamiliar codebases via voice. Explains how
#   files and folders connect and traces specific flows (auth, payments, etc.).
#   Runs automatically after indexing completes to produce a default "repo
#   overview" walkthrough, and responds to on-demand flow questions.
#
# Output structure (per AGENTS.md / PROMPTS_GUIDE.md):
#   {
#     "status": "ok | repo_too_large | insufficient_context",
#     "repo_summary": str,        # one-sentence repo description
#     "diagram_level": str,       # "file" | "folder"
#     "walkthrough": [
#       {
#         "sentence": str,             # max 20 words, declarative, voice-ready
#         "highlighted_nodes": list[str]  # IDs from diagram_node_ids only
#       },
#       ...                            # max 8 sentences (overview), 6 (flow)
#     ]
#   }
#
# Key constraints (per AGENTS.md):
#   - Only operates on repos with ≤ 100 indexed files; returns repo_too_large otherwise
#   - highlighted_nodes must only contain IDs present in diagram_node_ids
#   - On session start, runs automatically for "overview" without user asking
#   - Diagram level (file vs folder) drives which node IDs are valid

from __future__ import annotations

# Placeholder import — VectorStore will be wired in during Phase 5 implementation
# from ingestion.vector_store import VectorStore  # noqa: F401


class CodebaseExplorerAgent:
    """
    Guides developers through an unfamiliar codebase via voice.

    Produces an ordered, sentence-by-sentence walkthrough of either the full
    repository (when voice_query is "overview") or a specific code flow
    (e.g. "walk me through the auth flow"). Each sentence in the walkthrough
    is paired with a list of diagram node IDs (highlighted_nodes) that the
    frontend highlights in the Mermaid diagram in real time as Nova Sonic
    speaks the sentence.

    The audio stream's is_final flag is the master clock — the diagram never
    advances to the next highlighted_nodes set until is_final: true is received
    for the current sentence's audio chunk.

    Scope constraint (per ARCHITECTURE.md):
        Repos with more than 100 indexed files are rejected. The agent returns
        {"status": "repo_too_large", "message": "..."} and the Orchestrator
        surfaces this to the user by voice.

    Diagram level awareness:
        The agent receives diagram_level ("file" | "folder") and
        diagram_node_ids (the complete list of valid node IDs in the current
        diagram). It must only reference IDs from diagram_node_ids in
        highlighted_nodes — never fabricate or guess a node ID.

    System prompt:
        prompts/dev_mode/codebase_explorer.txt — version-controlled as core IP.
    """

    def run(self, context: dict) -> dict:
        """
        Execute the Codebase Explorer Agent for a single turn.

        Expected context keys (per AGENTS.md / codebase_explorer.txt):
            voice_query (str):          Developer's voice question, or "overview"
                                        for the automatic session-start walkthrough.
            file_tree (list[str]):      Complete list of all files/folders in the repo.
            import_graph (dict):        {file_path: [imported_file_paths]} from
                                        ingestion.repo_loader.build_import_graph().
            code_chunks (list[dict]):   FAISS-retrieved chunks relevant to voice_query.
            diagram_level (str):        "file" or "folder" — level of the current diagram.
            diagram_node_ids (list[str]): All valid node IDs in the current Mermaid diagram.

        Returns:
            {
                "status": "ok | repo_too_large | insufficient_context",
                "repo_summary": str,
                "diagram_level": str,
                "walkthrough": [
                    {"sentence": str, "highlighted_nodes": list[str]},
                    ...
                ]
            }

        Raises:
            NotImplementedError: Always — implementation deferred to Phase 5.
        """
        raise NotImplementedError(
            "Codebase Explorer Agent — implementation in Phase 5"
        )
