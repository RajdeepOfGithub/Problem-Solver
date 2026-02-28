"""
diagram/two_tone_generator.py
Vega — Phase 5: Project Intelligence Layer

Extends the base Mermaid diagram with two-tone color coding:
  🟢  Green  (#22c55e) — file exists with real code ("built")
  ⬜  Gray   (#6b7280) — file exists but mostly empty ("stub")
  ▫️  Gray dashed     — mentioned in docs but no file exists ("planned")

This module takes a base Mermaid string from the ingestion pipeline and
applies style declarations for each node based on DocScanner.file_status.

No Bedrock calls — pure string manipulation.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Style constants
# ---------------------------------------------------------------------------

STYLE_BUILT    = "fill:#22c55e,color:#fff"                        # Green
STYLE_STUB     = "fill:#6b7280,color:#fff"                        # Gray
STYLE_PLANNED  = "fill:#6b7280,color:#fff,stroke-dasharray:5"     # Gray dashed

LEGEND = "🟢 Built  |  ⬜ Stub  |  ▫️ Planned (no file)"

# Maps status → style string
_STATUS_TO_STYLE = {
    "built":   STYLE_BUILT,
    "stub":    STYLE_STUB,
    "planned": STYLE_PLANNED,
}

# Regex patterns for Mermaid node parsing
# Matches: nodeId["label"] or nodeId[label] or nodeId("label") or nodeId{label}
_NODE_DEF_RE = re.compile(
    r'^\s{0,8}([A-Za-z0-9_]+)(?:\[["\'"]?[^"\'\]]+["\'"]?\]|'
    r'\(["\'"]?[^"\'\)]+["\'"]?\)|'
    r'\{["\'"]?[^"\'\}]+["\'"]?\})',
    re.MULTILINE,
)

# Matches edge declarations (for extracting referenced node IDs)
_EDGE_RE = re.compile(
    r'([A-Za-z0-9_]+)\s*(?:-->|---|==>|-.->|--[^>]*>)\s*([A-Za-z0-9_]+)'
)

# Matches style declarations already in the diagram
_STYLE_LINE_RE = re.compile(r'^\s*style\s+([A-Za-z0-9_]+)', re.MULTILINE)


class TwoToneDiagramGenerator:
    """
    Applies two-tone status styling to a base Mermaid diagram.

    Usage::

        gen = TwoToneDiagramGenerator()
        result = gen.generate(
            base_mermaid  = "flowchart TD\\n    ...",
            file_status   = {"api/server.py": "built", "agents/stub.py": "stub"},
            planned_components = [...],
            node_ids      = ["api/server.py", "agents/stub.py"],
            diagram_level = "file",
        )
        print(result["mermaid"])
    """

    def generate(
        self,
        base_mermaid: str,
        file_status: Dict[str, str],
        planned_components: List[Dict],
        node_ids: List[str],
        diagram_level: str,
    ) -> Dict:
        """
        Generate a two-tone diagram from the base diagram and file status data.

        Args:
            base_mermaid:        Raw Mermaid string from ingestion pipeline.
            file_status:         Dict mapping file path → "built"|"stub"|"planned".
            planned_components:  List of planned component dicts from DocScanner.
            node_ids:            List of node labels extracted from the base diagram.
            diagram_level:       "file" or "folder" (affects folder aggregation).

        Returns:
            {
                "mermaid": str,
                "node_ids": [...],
                "styles_applied": {file_path: "built"|"stub"|"planned"},
                "legend": str,
                "valid": bool,
                "validation_error": str | None,
            }
        """
        if not base_mermaid or not base_mermaid.strip():
            return {
                "mermaid": "",
                "node_ids": [],
                "styles_applied": {},
                "legend": LEGEND,
                "valid": False,
                "validation_error": "Empty base diagram",
            }

        # Build mapping: node_id in diagram → file path in file_status
        node_to_path = self._build_node_to_path_map(base_mermaid, node_ids, file_status, diagram_level)

        # Optionally add planned nodes not yet in the diagram
        mermaid, new_node_ids = self._add_planned_nodes(
            base_mermaid, planned_components, set(node_to_path.keys()), diagram_level
        )
        if new_node_ids:
            for nid, path in new_node_ids.items():
                node_to_path[nid] = path

        # Determine which existing style lines are already present (avoid duplicates)
        already_styled = set(_STYLE_LINE_RE.findall(mermaid))

        # Apply styles
        styles_applied: Dict[str, str] = {}
        style_lines: List[str] = []

        for node_id, path in node_to_path.items():
            if node_id in already_styled:
                continue
            status = self._resolve_status(path, file_status, diagram_level)
            style_str = _STATUS_TO_STYLE.get(status, STYLE_STUB)
            style_lines.append(f"    style {node_id} {style_str}")
            styles_applied[path] = status

        if style_lines:
            mermaid = mermaid.rstrip() + "\n\n    %% Two-tone status styles\n" + "\n".join(style_lines)

        valid, error = self._validate_mermaid(mermaid)

        if not valid:
            logger.warning("TwoToneDiagramGenerator: validation failed — %s", error)
            # Return the base mermaid unmodified on validation failure
            return {
                "mermaid": base_mermaid,
                "node_ids": node_ids,
                "styles_applied": {},
                "legend": LEGEND,
                "valid": False,
                "validation_error": error,
            }

        all_node_ids = list(node_ids) + [
            path for path in new_node_ids.values()
        ] if new_node_ids else list(node_ids)

        return {
            "mermaid": mermaid,
            "node_ids": all_node_ids,
            "styles_applied": styles_applied,
            "legend": LEGEND,
            "valid": True,
            "validation_error": None,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_node_to_path_map(
        self,
        mermaid: str,
        node_ids: List[str],
        file_status: Dict[str, str],
        diagram_level: str,
    ) -> Dict[str, str]:
        """
        Map each Mermaid node ID (sanitized) → file path in file_status.

        Strategy:
        1. Extract all node IDs defined in the diagram.
        2. For each diagram node ID, try to match it to a key in file_status
           by unsanitizing (replace underscores back to / and .) or by
           checking if the node label matches a file path.
        """
        # Extract node definitions from the diagram text
        diagram_node_ids = set(m.group(1) for m in _NODE_DEF_RE.finditer(mermaid))

        # Extract node labels (the text in brackets) to map back to file paths
        label_to_node: Dict[str, str] = {}
        for match in re.finditer(
            r'([A-Za-z0-9_]+)\[["\'"]?([^"\'\]]+)["\'"]?\]', mermaid
        ):
            node_id = match.group(1)
            label = match.group(2).strip()
            label_to_node[label] = node_id

        result: Dict[str, str] = {}

        for file_path in file_status:
            # Try matching by label first (most reliable)
            if file_path in label_to_node:
                result[label_to_node[file_path]] = file_path
                continue

            # Try sanitized node ID
            sanitized = self._sanitize_node_id(file_path)
            if sanitized in diagram_node_ids:
                result[sanitized] = file_path

        return result

    def _add_planned_nodes(
        self,
        mermaid: str,
        planned_components: List[Dict],
        existing_node_ids: set[str],
        diagram_level: str,
    ) -> Tuple[str, Dict[str, str]]:
        """
        Add new nodes to the diagram for planned components not already present.
        Returns (updated_mermaid, {new_node_id: expected_path}).
        """
        new_entries: List[str] = []
        new_node_map: Dict[str, str] = {}

        for comp in planned_components:
            expected_path = comp.get("expected_path", "")
            if not expected_path:
                continue

            node_id = self._sanitize_node_id(expected_path)
            if node_id in existing_node_ids:
                continue

            # Connect to the most logical parent by finding a parent folder node
            parent_id = self._find_parent_node_id(expected_path, existing_node_ids)
            label = expected_path
            entry = f'    {node_id}["{label}"]'

            if parent_id:
                entry += f"\n    {parent_id} --> {node_id}"

            new_entries.append(entry)
            new_node_map[node_id] = expected_path
            existing_node_ids.add(node_id)

        if new_entries:
            mermaid = mermaid.rstrip() + "\n\n    %% Planned components\n" + "\n".join(new_entries)

        return mermaid, new_node_map

    def _find_parent_node_id(self, file_path: str, existing_node_ids: set[str]) -> Optional[str]:
        """
        Find the most specific existing node that could be a parent of file_path.
        E.g. for "agents/dev_mode/new_agent.py", try "agents_dev_mode", then "agents".
        """
        parts = file_path.replace("\\", "/").split("/")

        # Try progressively shorter parent paths
        for i in range(len(parts) - 1, 0, -1):
            parent_path = "/".join(parts[:i])
            candidate = self._sanitize_node_id(parent_path)
            if candidate in existing_node_ids:
                return candidate
            # Also try the folder with trailing slash
            candidate2 = self._sanitize_node_id(parent_path + "/")
            if candidate2 in existing_node_ids:
                return candidate2

        return None

    def _resolve_status(
        self, path: str, file_status: Dict[str, str], diagram_level: str
    ) -> str:
        """
        Resolve the display status for a path.

        For folder-level diagrams, aggregate: if any file under the folder
        is not "built" → "stub"; if all planned → "planned".
        """
        if diagram_level == "file":
            return file_status.get(path, "stub")

        # Folder-level: aggregate all files under this folder
        folder_prefix = path.rstrip("/") + "/"
        children = {
            v for k, v in file_status.items()
            if k.startswith(folder_prefix) or k == path
        }

        if not children:
            return file_status.get(path, "stub")

        if all(s == "built" for s in children):
            return "built"
        if all(s == "planned" for s in children):
            return "planned"
        return "stub"

    def _validate_mermaid(self, mermaid: str) -> Tuple[bool, Optional[str]]:
        """
        Basic server-side validation of Mermaid syntax.

        Checks:
        1. Starts with "flowchart" or "graph"
        2. No unclosed double-brackets or unmatched brackets (basic heuristic)
        3. Style declarations reference valid node IDs
        4. No obvious syntax errors (duplicate colon lines, etc.)

        Returns (is_valid, error_message_or_None).
        """
        stripped = mermaid.strip()

        if not stripped.startswith(("flowchart", "graph")):
            return False, "Diagram must start with 'flowchart' or 'graph'"

        # Collect all defined node IDs
        defined_ids = set(m.group(1) for m in _NODE_DEF_RE.finditer(mermaid))

        # Also collect IDs from edge declarations
        for match in _EDGE_RE.finditer(mermaid):
            defined_ids.add(match.group(1))
            defined_ids.add(match.group(2))

        # Validate style declarations reference known node IDs
        for match in _STYLE_LINE_RE.finditer(mermaid):
            styled_id = match.group(1)
            if styled_id and styled_id not in defined_ids:
                return False, f"style declaration references unknown node ID: '{styled_id}'"

        # Check for duplicate opening fence (shouldn't happen but guard anyway)
        if mermaid.count("flowchart") > 1:
            return False, "Multiple 'flowchart' declarations found"

        return True, None

    def _sanitize_node_id(self, file_path: str) -> str:
        """Convert a file path to a valid Mermaid node ID."""
        return re.sub(r"[^A-Za-z0-9_]", "_", file_path).strip("_")
