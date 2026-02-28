"""
ingestion/repo_loader.py
Vega — Phase 2: Core Intelligence Layer

Clones a GitHub repository, walks the file tree, and chunks files into
segments with full source metadata. Output format matches the chunk schema
that all downstream agents expect:
  { file, start_line, end_line, content, language, repo_url, branch }

Also builds an import graph for Python files:
  { relative_file_path: [list_of_imported_relative_file_paths] }
Circular imports are detected and the closing edge is dropped (with a warning).

Design rules:
- API-first: uses GitPython + GitHub REST, no Nova Act
- Chunking preserves logical boundaries (functions/classes where possible,
  fixed-size with overlap as fallback)
- Skips binary, generated, and vendor files
- Returns (chunks, import_graph) — consumed directly by embeddings.py
  and diagram_generator.py
- Hard limit: raises ValueError if the repo exceeds FILE_LIMIT indexable files
"""

from __future__ import annotations

import os
import re
import tempfile
import logging
from pathlib import Path
from typing import Iterator

import git  # gitpython

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hard file limit enforced before chunking begins
FILE_LIMIT = 100

# File extensions to index — maps extension → language label
SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".py":   "python",
    ".js":   "javascript",
    ".ts":   "typescript",
    ".tsx":  "typescript",
    ".jsx":  "javascript",
    ".java": "java",
    ".go":   "go",
    ".rs":   "rust",
    ".rb":   "ruby",
    ".cs":   "csharp",
    ".cpp":  "cpp",
    ".c":    "c",
    ".h":    "c",
    ".hpp":  "cpp",
    ".sh":   "bash",
    ".yaml": "yaml",
    ".yml":  "yaml",
    ".json": "json",
    ".md":   "markdown",
    ".txt":  "text",
    ".toml": "toml",
    ".tf":   "terraform",
    ".sql":  "sql",
    # Dependency manifests — always included for Security Audit Agent
    "requirements.txt":  "text",
    "package.json":      "json",
    "Pipfile":           "text",
    "go.mod":            "text",
    "Gemfile":           "text",
    "pom.xml":           "xml",
    "build.gradle":      "groovy",
    "Cargo.toml":        "toml",
}

# Directories to skip entirely
SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".github", "__pycache__", "node_modules", ".venv", "venv",
    "env", ".env", "dist", "build", "target", ".idea", ".vscode",
    "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "vendor", "third_party", "site-packages",
})

# Files to skip by name pattern
SKIP_FILE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\.pyc$"),
    re.compile(r"\.min\.(js|css)$"),          # minified assets
    re.compile(r"package-lock\.json$"),        # generated lockfiles
    re.compile(r"yarn\.lock$"),
    re.compile(r"poetry\.lock$"),
    re.compile(r"Pipfile\.lock$"),
    re.compile(r"\.(png|jpg|jpeg|gif|svg|ico|woff|woff2|ttf|eot|pdf|zip|tar|gz)$"),
]

# Chunk tuning
CHUNK_LINES      = 80    # target lines per chunk
CHUNK_OVERLAP    = 10    # lines of overlap between adjacent chunks
MAX_FILE_LINES   = 5000  # files larger than this are still chunked but logged

# Python import statement patterns
# NOTE: use [ \t] (not \s) in the import list to avoid consuming newlines
_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([\w.]+)[ \t]+import|import[ \t]+([\w.,\t ]+))",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_repo(
    repo_url: str,
    branch: str = "main",
    github_token: str | None = None,
) -> tuple[list[dict], dict[str, list[str]]]:
    """
    Clone *repo_url* at *branch*, walk all supported files, chunk them, and
    return a tuple of (chunks, import_graph).

    Raises ValueError if the repo exceeds FILE_LIMIT (100) indexable files —
    count is checked after cloning but before chunking begins.

    Each chunk dict:
        {
            "file":       str,   # path relative to repo root
            "start_line": int,   # 1-indexed, inclusive
            "end_line":   int,   # 1-indexed, inclusive
            "content":    str,   # raw source text of this chunk
            "language":   str,   # language label
            "repo_url":   str,   # original repo URL (stripped of token)
            "branch":     str,
        }

    import_graph:
        { relative_file_path: [list of repo-relative paths it imports] }
        Only includes imports that resolve to actual files in this repo.
        Circular imports are broken with a warning log.

    Raises:
        ValueError  — if repo_url is invalid or file count exceeds limit
        git.GitCommandError — if clone fails (bad token, repo not found, etc.)
    """
    repo_url = _validate_repo_url(repo_url)
    clone_url = _inject_token(repo_url, github_token)

    with tempfile.TemporaryDirectory(prefix="vega_repo_") as tmpdir:
        logger.info("Cloning %s @ %s into %s", repo_url, branch, tmpdir)
        git.Repo.clone_from(
            clone_url,
            tmpdir,
            branch=branch,
            depth=1,
            single_branch=True,
        )
        logger.info("Clone complete. Counting indexable files…")

        root = Path(tmpdir)
        all_files = list(_walk_files(root))

        _check_file_limit(len(all_files), repo_url)

        chunks: list[dict] = []
        for file_path, language in all_files:
            rel_path = str(file_path.relative_to(root))
            chunks.extend(_chunk_file(file_path, rel_path, language, repo_url, branch))

        logger.info("Loaded %d chunks from %d file(s)", len(chunks), len(all_files))

        py_files = {str(fp.relative_to(root)) for fp, lang in all_files if lang == "python"}
        import_graph = build_import_graph(root, list(py_files))

        return chunks, import_graph


def load_repo_to_dir(
    repo_url: str,
    target_dir: str,
    branch: str = "main",
    github_token: str | None = None,
) -> tuple[list[dict], str, dict[str, list[str]]]:
    """
    Like load_repo() but keeps the cloned repo on disk at *target_dir* for
    incremental re-indexing. Returns (chunks, repo_root_path, import_graph).

    Raises ValueError if the repo exceeds FILE_LIMIT indexable files.
    """
    repo_url = _validate_repo_url(repo_url)
    clone_url = _inject_token(repo_url, github_token)

    target = Path(target_dir)
    if target.exists():
        logger.info("Target dir exists — pulling latest at %s", target)
        repo = git.Repo(target)
        repo.remotes.origin.pull()
    else:
        logger.info("Cloning %s @ %s into %s", repo_url, branch, target)
        target.mkdir(parents=True, exist_ok=True)
        git.Repo.clone_from(clone_url, str(target), branch=branch, depth=1, single_branch=True)

    logger.info("Counting indexable files…")
    all_files = list(_walk_files(target))

    _check_file_limit(len(all_files), repo_url)

    chunks: list[dict] = []
    for file_path, language in all_files:
        rel_path = str(file_path.relative_to(target))
        chunks.extend(_chunk_file(file_path, rel_path, language, repo_url, branch))

    logger.info("Loaded %d chunks from cloned repo at %s", len(chunks), target)

    py_files = {str(fp.relative_to(target)) for fp, lang in all_files if lang == "python"}
    import_graph = build_import_graph(target, list(py_files))

    return chunks, str(target), import_graph


def build_import_graph(
    repo_root: Path,
    py_file_paths: list[str],
) -> dict[str, list[str]]:
    """
    Build a Python import graph from the given list of repo-relative .py paths.

    Returns:
        { file_path: [list_of_imported_file_paths_within_this_repo] }

    Only includes imports that resolve to actual files in the repo.
    Stdlib and third-party imports are silently skipped.
    Circular imports are detected; the edge that closes a cycle is dropped
    with a WARNING log entry.

    Args:
        repo_root:     Absolute path to the cloned/checked-out repo root.
        py_file_paths: List of repo-relative paths to .py files.
    """
    py_file_set = set(py_file_paths)
    raw_graph: dict[str, list[str]] = {}

    for rel_path in py_file_paths:
        abs_path = repo_root / rel_path
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("Could not read %s for import analysis: %s", abs_path, exc)
            raw_graph[rel_path] = []
            continue

        imports = _extract_py_imports(content, rel_path, py_file_set)
        raw_graph[rel_path] = imports

    return _break_cycles(raw_graph)


def chunk_file_content(
    content: str,
    rel_path: str,
    language: str,
    repo_url: str = "",
    branch: str = "main",
) -> list[dict]:
    """
    Utility: chunk a string of file content directly (no filesystem access).
    Useful for testing and for re-chunking files already in memory.
    """
    return _chunk_lines(content.splitlines(), rel_path, language, repo_url, branch)


# ---------------------------------------------------------------------------
# Internal helpers — file limit
# ---------------------------------------------------------------------------

def _check_file_limit(count: int, repo_url: str) -> None:
    if count > FILE_LIMIT:
        raise ValueError(
            f"Repository exceeds 100 file limit ({count} files found). "
            "Try indexing a specific subdirectory instead."
        )
    logger.info("File count: %d (within %d-file limit)", count, FILE_LIMIT)


# ---------------------------------------------------------------------------
# Internal helpers — import graph
# ---------------------------------------------------------------------------

def _extract_py_imports(
    content: str,
    rel_path: str,
    py_file_set: set[str],
) -> list[str]:
    """
    Parse all import/from-import statements in *content* and return a
    deduplicated list of repo-relative file paths that are imported and
    actually exist in *py_file_set*.
    """
    file_dir = str(Path(rel_path).parent)
    resolved: list[str] = []
    seen: set[str] = set()

    for match in _IMPORT_RE.finditer(content):
        from_module = match.group(1)    # from X import ...
        import_list = match.group(2)    # import X, Y, Z

        if from_module:
            modules = [from_module.strip()]
        else:
            modules = [
                part.strip().split(" as ")[0].strip()
                for part in import_list.split(",")
                if part.strip()
            ]

        for module in modules:
            if not module:
                continue
            path = _resolve_module(module, file_dir, py_file_set)
            if path and path != rel_path and path not in seen:
                resolved.append(path)
                seen.add(path)

    return resolved


def _resolve_module(
    module: str,
    file_dir: str,
    py_file_set: set[str],
) -> str | None:
    """
    Try to resolve a module name to a repo-relative .py file path.

    Handles:
    - Absolute imports: `import auth.handler` → auth/handler.py
    - Relative imports: `from .utils import X` → sibling utils.py
    - Package imports: `import auth` → auth/__init__.py

    Returns None if the module cannot be resolved to a file in the repo
    (i.e. it's stdlib or third-party).
    """
    # Relative imports — resolve from current file directory
    if module.startswith("."):
        dots = len(module) - len(module.lstrip("."))
        mod_name = module.lstrip(".")

        # Walk up the directory tree by the number of leading dots
        parts = file_dir.split("/") if file_dir and file_dir != "." else []
        if dots > 1:
            parts = parts[:-(dots - 1)] if len(parts) >= dots - 1 else []

        base = "/".join(parts) if parts else ""
        return _try_resolve(mod_name, base, py_file_set)

    # Absolute import
    return _try_resolve(module, "", py_file_set)


def _try_resolve(
    mod_name: str,
    base_dir: str,
    py_file_set: set[str],
) -> str | None:
    """
    Given a dotted module name and a base directory, try:
    1. base_dir/mod/name.py
    2. base_dir/mod/name/__init__.py
    3. mod/name.py  (absolute, ignoring base_dir)
    4. mod/name/__init__.py
    """
    parts = mod_name.replace(".", "/") if mod_name else ""

    candidates: list[str] = []
    if base_dir:
        candidates += [
            f"{base_dir}/{parts}.py",
            f"{base_dir}/{parts}/__init__.py",
        ]
    if parts:
        candidates += [
            f"{parts}.py",
            f"{parts}/__init__.py",
        ]

    for candidate in candidates:
        # Normalise path separators and double-slashes
        norm = "/".join(p for p in candidate.split("/") if p)
        if norm in py_file_set:
            return norm

    return None


# ---------------------------------------------------------------------------
# Internal helpers — cycle detection
# ---------------------------------------------------------------------------

def _break_cycles(graph: dict[str, list[str]]) -> dict[str, list[str]]:
    """
    Perform DFS on *graph* and remove any edge that closes a cycle.
    Logs a WARNING for each removed edge.

    Returns a new graph dict with the same keys but cycle-free adjacency lists.
    """
    result: dict[str, list[str]] = {k: list(v) for k, v in graph.items()}
    visited: set[str] = set()
    rec_stack: set[str] = set()

    def dfs(node: str) -> None:
        visited.add(node)
        rec_stack.add(node)

        for neighbor in list(result.get(node, [])):
            if neighbor not in visited:
                # Ensure the neighbour has an entry so DFS can recurse into it
                if neighbor not in result:
                    result[neighbor] = []
                dfs(neighbor)
            elif neighbor in rec_stack:
                # This edge closes a cycle — remove it
                logger.warning(
                    "Circular import detected: %s → %s. "
                    "Breaking cycle by removing this import edge.",
                    node, neighbor,
                )
                result[node].remove(neighbor)

        rec_stack.discard(node)

    for node in list(result.keys()):
        if node not in visited:
            dfs(node)

    return result


# ---------------------------------------------------------------------------
# Internal helpers — file walking and chunking
# ---------------------------------------------------------------------------

def _validate_repo_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not re.match(r"^https://github\.com/[\w.\-]+/[\w.\-]+(\.git)?$", url):
        raise ValueError(
            f"Invalid repo URL: {url!r}. "
            "Must be an HTTPS GitHub URL, e.g. https://github.com/org/repo"
        )
    return url.removesuffix(".git")


def _inject_token(repo_url: str, token: str | None) -> str:
    if not token:
        return repo_url
    return repo_url.replace("https://", f"https://{token}@", 1)


def _walk_files(root: Path) -> Iterator[tuple[Path, str]]:
    """Yield (absolute_path, language) for every indexable file under root."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        for filename in filenames:
            if any(p.search(filename) for p in SKIP_FILE_PATTERNS):
                continue

            filepath = Path(dirpath) / filename

            language = (
                SUPPORTED_EXTENSIONS.get(filename)
                or SUPPORTED_EXTENSIONS.get(filepath.suffix.lower())
            )
            if language is None:
                continue

            yield filepath, language


def _chunk_file(
    filepath: Path,
    rel_path: str,
    language: str,
    repo_url: str,
    branch: str,
) -> list[dict]:
    """Read a file and return its chunks. Skips unreadable/binary files."""
    try:
        raw = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Could not read %s: %s", filepath, exc)
        return []

    lines = raw.splitlines()
    if not lines:
        return []

    if len(lines) > MAX_FILE_LINES:
        logger.info("Large file (%d lines): %s — chunking with fixed windows", len(lines), rel_path)

    return _chunk_lines(lines, rel_path, language, repo_url, branch)


def _chunk_lines(
    lines: list[str],
    rel_path: str,
    language: str,
    repo_url: str,
    branch: str,
) -> list[dict]:
    """
    Chunk a list of lines into overlapping windows.

    Strategy:
    1. For Python: try to split at top-level function/class boundaries first.
    2. Fallback: fixed CHUNK_LINES window with CHUNK_OVERLAP overlap.
    """
    if language == "python":
        chunks = _chunk_python_logical(lines, rel_path, repo_url, branch)
        if chunks:
            return chunks

    return _chunk_fixed_window(lines, rel_path, language, repo_url, branch)


def _chunk_fixed_window(
    lines: list[str],
    rel_path: str,
    language: str,
    repo_url: str,
    branch: str,
) -> list[dict]:
    """Simple fixed-window chunking with overlap."""
    chunks = []
    total = len(lines)
    start = 0

    while start < total:
        end = min(start + CHUNK_LINES, total)
        content = "\n".join(lines[start:end])

        if content.strip():
            chunks.append({
                "file":       rel_path,
                "start_line": start + 1,
                "end_line":   end,
                "content":    content,
                "language":   language,
                "repo_url":   repo_url,
                "branch":     branch,
            })

        if end >= total:
            break
        start = end - CHUNK_OVERLAP

    return chunks


def _chunk_python_logical(
    lines: list[str],
    rel_path: str,
    repo_url: str,
    branch: str,
) -> list[dict]:
    """
    Split Python files at top-level def/class boundaries.
    Returns [] if the file has fewer than 2 top-level definitions (falls back
    to fixed-window).
    """
    boundary_pattern = re.compile(r"^(def |class |async def )")
    boundaries: list[int] = [
        i for i, line in enumerate(lines)
        if boundary_pattern.match(line)
    ]

    if len(boundaries) < 2:
        return []

    chunks = []
    total = len(lines)
    segments = list(zip(boundaries, boundaries[1:] + [total]))

    for seg_start, seg_end in segments:
        if seg_end - seg_start > CHUNK_LINES * 2:
            sub = _chunk_fixed_window(
                lines[seg_start:seg_end],
                rel_path,
                "python",
                repo_url,
                branch,
            )
            for c in sub:
                c["start_line"] += seg_start
                c["end_line"]   += seg_start
            chunks.extend(sub)
        else:
            content = "\n".join(lines[seg_start:seg_end])
            if content.strip():
                chunks.append({
                    "file":       rel_path,
                    "start_line": seg_start + 1,
                    "end_line":   seg_end,
                    "content":    content,
                    "language":   "python",
                    "repo_url":   repo_url,
                    "branch":     branch,
                })

    return chunks


def _count_unique_files(chunks: list[dict]) -> int:
    return len({c["file"] for c in chunks})
