# 🤖 Vega — Agent Specifications

> See also: [[ARCHITECTURE]] | [[PROMPTS_GUIDE]] | [[Roadmap]]

---

Vega uses 9 agents total — 1 Orchestrator and 8 specialized agents split across Dev Mode and Ops Mode. All reasoning agents use Nova 2 Lite via Amazon Bedrock. All agents run within the AWS Strands Agents or LangGraph orchestration framework and communicate exclusively through the Orchestrator — sub-agents never call each other directly. System prompts for every agent live in `prompts/` and are version-controlled as core IP.

---

## Orchestrator Agent

> [!NOTE] Role
> The central routing brain of Vega — receives every transcribed voice input, classifies intent, maintains session memory, and spawns the appropriate mode agents with full context.

| Property        | Value                                                                                                                                                                                       |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Mode            | Both                                                                                                                                                                                        |
| Model           | Nova 2 Lite                                                                                                                                                                                 |
| Input           | Transcribed voice text (string) + session memory object                                                                                                                                     |
| Output          | Structured task object dispatched to Dev Mode or Ops Mode agents                                                                                                                            |
| Tools Available | Intent classifier, session memory store, agent spawner, Nova Sonic confirmation trigger                                                                                                     |
| Spawned By      | System (on session start via `POST /session/start`)                                                                                                                                         |
| Spawns          | Code Review Agent, Security Audit Agent, Architecture Analysis Agent, PR Review Agent (Dev Mode) / Incident Analysis Agent, Log Parsing Agent, Root Cause Agent, Fix Draft Agent (Ops Mode) |

**Behavior Notes:**
- Classifies every input into one of: `dev_code_review`, `dev_security_audit`, `dev_architecture`, `dev_pr_review`, `ops_incident`, `ops_followup`, or `ambiguous`. On `ambiguous`, asks a clarifying voice question before spawning any agents.
- Maintains a rolling context window of the last 10 turns per session. Context is injected into every sub-agent call so agents can reference prior findings without re-querying the knowledge base.
- On receiving sub-agent results, the Orchestrator ranks all findings by severity (CRITICAL → HIGH → MEDIUM → LOW), deduplicates overlapping findings, and selects the top 3–5 items for the voice response to avoid overwhelming the developer.

---

## Code Review Agent

> [!NOTE] Role
> Analyzes retrieved code chunks for quality issues — complexity, anti-patterns, naming conventions, duplication, and maintainability — and returns a ranked findings list with specific line references.

| Property | Value |
|---|---|
| Mode | Dev |
| Model | Nova 2 Lite |
| Input | Code chunks (from knowledge base retrieval) + original voice query + file path metadata |
| Output | JSON findings list with `file`, `line`, `severity`, `category`, and `description` per finding |
| Tools Available | Knowledge base retrieval (via Orchestrator context), no external API calls |
| Spawned By | Orchestrator |
| Spawns | None |

**Behavior Notes:**
- Only flags issues that are directly evidenced in the provided code chunks. Never speculates about code that was not retrieved.
- Severity levels: CRITICAL (logic errors, broken behavior), HIGH (significant technical debt), MEDIUM (maintainability concerns), LOW (style/naming). Does not use any other severity labels.
- If the retrieved chunks are insufficient to give a meaningful review, returns a `{"status": "insufficient_context", "message": "..."}` response rather than guessing.

---

## Security Audit Agent

> [!NOTE] Role
> Scans retrieved code and dependency context for security vulnerabilities across the OWASP Top 10, exposed secrets, insecure configurations, and vulnerable dependency versions — returning a severity-ranked vulnerability report.

| Property | Value |
|---|---|
| Mode | Dev |
| Model | Nova 2 Lite |
| Input | Code chunks + dependency file contents (requirements.txt, package.json, etc.) + file path metadata |
| Output | JSON vulnerability report: list of findings each with `cve_reference` (if applicable), `owasp_category`, `severity`, `file`, `line`, `description`, `remediation` |
| Tools Available | Knowledge base retrieval (via Orchestrator context) |
| Spawned By | Orchestrator |
| Spawns | None |

**Behavior Notes:**
- OWASP Top 10 categories the agent checks against: Injection, Broken Authentication, Sensitive Data Exposure, XML External Entities, Broken Access Control, Security Misconfiguration, XSS, Insecure Deserialization, Using Components with Known Vulnerabilities, Insufficient Logging.
- Exposed secrets (API keys, tokens, passwords hardcoded in source) are always flagged as CRITICAL regardless of context.
- Vulnerability report is always sorted CRITICAL first. The voice response spoken by Vega covers only CRITICAL and HIGH items — MEDIUM and LOW are included in the full JSON but not read aloud to avoid information overload.

---

## Architecture Analysis Agent

> [!NOTE] Role
> Evaluates the structural design of a codebase — coupling between modules, cohesion within components, adherence to design patterns, and scalability concerns — and returns concrete, actionable improvement suggestions.

| Property | Value |
|---|---|
| Mode | Dev |
| Model | Nova 2 Lite |
| Input | Code structure metadata (file tree, import graph) + README content + architecture diagram embeddings (if indexed) + relevant code chunks |
| Output | JSON assessment with `overall_health` score (1–10), `concerns` list, `patterns_identified` list, and `suggestions` list each with `priority` and `rationale` |
| Tools Available | Knowledge base retrieval (via Orchestrator context) |
| Spawned By | Orchestrator |
| Spawns | None |

**Behavior Notes:**
- When architecture diagrams are not indexed, the agent notes this explicitly in its response rather than inferring structure from code alone — do not fill gaps with assumptions.
- Suggestions must be specific and actionable: not "improve cohesion" but "the `AuthService` class handles token generation, user validation, and email sending — split into three classes with single responsibilities".
- For the demo golden path, this agent is not in the primary flow — Security Audit Agent takes priority for Dev Mode. Architecture Analysis is available as a follow-up if the developer asks.

---

## PR Review Agent

> [!NOTE] Role
> Reviews a pull request diff against the existing codebase context, checking for correctness, test coverage gaps, breaking changes, and style consistency — returning a structured review with actionable inline comments.

| Property | Value |
|---|---|
| Mode | Dev |
| Model | Nova 2 Lite |
| Input | PR diff (unified diff format) + knowledge base chunks from modified files + PR description |
| Output | JSON review object: `summary` string, `verdict` (approve/request_changes/comment), `inline_comments` list each with `file`, `line`, `body` |
| Tools Available | Knowledge base retrieval (via Orchestrator context), GitHub API (read PR diff) |
| Spawned By | Orchestrator |
| Spawns | None |

**Behavior Notes:**
- The `verdict` field drives the GitHub API call — `approve` triggers a PR approval, `request_changes` posts a review requesting changes, `comment` posts without a blocking verdict. All three require voice confirmation before executing.
- Breaking change detection is based on: changed public function signatures, removed exports, modified database schema files, changed environment variable names. Flag any of these as HIGH or CRITICAL.
- If no tests are present or test coverage appears to drop based on the diff, always include a MEDIUM finding in `inline_comments` at the most relevant modified file.

---

## Incident Analysis Agent

> [!NOTE] Role
> Receives a developer's voice description of a production incident and extracts a structured incident object — identifying the affected service, estimated time window, and severity classification — to initialize the Ops Mode investigation pipeline.

| Property | Value |
|---|---|
| Mode | Ops |
| Model | Nova 2 Lite |
| Input | Transcribed voice description of the incident (e.g., "My Lambda auth function has been returning 500s since about 2pm") |
| Output | JSON incident object: `service` (e.g., "lambda:auth-service"), `time_window_start`, `time_window_end`, `severity` (P1/P2/P3), `description`, `next_action` |
| Tools Available | Session memory (to ask clarifying questions if service or time window is ambiguous) |
| Spawned By | Orchestrator |
| Spawns | Log Parsing Agent (passes incident object as input) |

**Behavior Notes:**
- If the developer's description does not include a time window, the agent defaults to the last 1 hour and notes this assumption in the response so it can be corrected.
- Severity classification: P1 = production down or data loss risk; P2 = degraded production with workaround; P3 = intermittent or non-critical production issue.
- The `next_action` field in the output tells the Orchestrator what to do next: always `"retrieve_logs"` after incident classification — this triggers the Log Parsing Agent with the structured incident object.

---

## Log Parsing Agent

> [!NOTE] Role
> Receives raw CloudWatch logs (or other AWS service logs) retrieved by Boto3, parses them for error patterns, exception traces, and anomalous event sequences, and returns a structured summary that the Root Cause Agent can reason over.

| Property | Value |
|---|---|
| Mode | Ops |
| Model | Nova 2 Lite |
| Input | Raw log JSON from Boto3 (CloudWatch `filter_log_events` / `get_log_events` response) + incident object (service, time window) |
| Output | JSON log summary: `error_count`, `warning_count`, `key_events` list (each with `timestamp`, `level`, `message`, `trace`), `anomaly_patterns` list |
| Tools Available | Boto3 CloudWatch log retrieval (via `actions/aws_actions.py`) |
| Spawned By | Incident Analysis Agent (via Orchestrator) |
| Spawns | Root Cause Agent (passes log summary as input) |

**Behavior Notes:**
- The agent must filter noise aggressively — only surface events that are ERROR or CRITICAL level, or INFO events that appear in anomalous clusters (e.g., a request that normally completes in 200ms taking 30s).
- Exception stack traces must be extracted in full — truncated traces lose the file and line number information that the Root Cause Agent needs.
- If Boto3 returns an empty log set for the requested time window, the agent widens the window by 30 minutes and retries once before returning a `{"status": "no_logs_found"}` response.

---

## Root Cause Agent

> [!NOTE] Role
> Correlates parsed log events with relevant code chunks from the knowledge base to trace the incident back to a specific file, line number, function, or recent commit — producing a human-readable root cause statement with evidence.

| Property | Value |
|---|---|
| Mode | Ops |
| Model | Nova 2 Lite |
| Input | Parsed log summary (from Log Parsing Agent) + knowledge base chunks for the affected service + git blame / recent commit metadata (if available) |
| Output | JSON root cause object: `root_cause_statement` (plain English), `evidence` list (each with `file`, `line`, `log_event`, `explanation`), `confidence` (high/medium/low), `suspected_commit` (optional) |
| Tools Available | Knowledge base retrieval (via Orchestrator context) |
| Spawned By | Log Parsing Agent (via Orchestrator) |
| Spawns | Fix Draft Agent (passes root cause object as input) |

**Behavior Notes:**
- `confidence` must be set honestly: `high` only when both a log error and a specific code line are directly correlated; `medium` when correlation is likely but not proven; `low` when only circumstantial evidence is available. Never report `high` confidence on a guess.
- The `root_cause_statement` is what Nova Sonic speaks aloud — it must be written in clear, direct prose that is understandable when heard (no code snippets, no markdown, no lists in this field).
- If the confidence is `low`, the agent must say so in the voice response and ask the developer if they want to expand the log search window or check a different service before generating a fix.

---

## Fix Draft Agent

> [!NOTE] Role
> Takes the root cause analysis and affected code chunks and generates a proposed code fix with a plain English explanation and a confidence score — outputting a diff-style patch ready for GitHub PR creation.

| Property | Value |
|---|---|
| Mode | Ops |
| Model | Nova 2 Lite |
| Input | Root cause object (from Root Cause Agent) + affected code chunks from knowledge base + file path metadata |
| Output | JSON fix object: `fix_diff` (unified diff string), `explanation` (plain English, voice-ready), `confidence_score` (0.0–1.0), `files_modified` list, `warnings` list (any side effects or risks) |
| Tools Available | Knowledge base retrieval (via Orchestrator context), GitHub API (create draft PR — requires voice confirmation) |
| Spawned By | Root Cause Agent (via Orchestrator) |
| Spawns | None |

**Behavior Notes:**
- `confidence_score` below 0.6 must trigger a warning in the voice response: *"I have a proposed fix but my confidence is low. I recommend reviewing it manually before merging."*
- The `explanation` field is what Nova Sonic reads aloud — it must be a maximum of 3 sentences, written for audio comprehension (no technical jargon without explanation, no code in this field).
- The `warnings` list must always include any functions, services, or environment variables that the fix might affect beyond the directly modified file — even if the risk is low. Judges and developers will check this.

---

## Codebase Explorer Agent

> [!NOTE] Role
> Guides developers through an unfamiliar codebase via voice — explaining how files and folders connect, tracing specific flows like auth or payments, and driving real-time diagram highlighting in sync with speech.

| Property | Value |
|---|---|
| Mode | Dev |
| Model | Nova 2 Lite |
| Input | Voice query (e.g. "walk me through the auth flow") + full file tree + import graph + FAISS knowledge base chunks for relevant files |
| Output | JSON response with an ordered array of sentences, each paired with the diagram node IDs to highlight while that sentence plays |
| Tools Available | Knowledge base retrieval (via Orchestrator context) |
| Spawned By | Orchestrator (on session start automatically for repo overview, or on demand for specific flow questions) |
| Spawns | None |

**Behavior Notes:**
- On session start, this agent runs automatically after indexing completes and produces a default "repo overview" walkthrough without the user asking — giving them immediate value the moment the repo is ready.
- Every response is structured as an ordered sentence list. Each sentence has exactly one `highlighted_nodes` array. The frontend advances through this list using the audio `is_final` flag as the clock — the diagram never advances ahead of the voice.
- Only highlights nodes that exist in the diagram. If a concept cannot be mapped to a specific file node (e.g. "retry logic inside a function"), the agent answers verbally and returns an empty `highlighted_nodes` array for that sentence rather than guessing a node.
- Scope constraint: only operates on repos with 100 files or fewer. If the indexed repo exceeds 100 files, returns a `{"status": "repo_too_large", "message": "..."}` response and the Orchestrator surfaces this to the user by voice.
- Diagram is rendered at folder level for repos over 30 files, file level for repos under 30 files. The agent respects whatever level the diagram was generated at and only references node IDs that exist at that level.

---

## 🆕 Project Intelligence Agent

> [!NOTE] Role
> Runs automatically on session start after the two-tone diagram is generated. Cross-references the indexed codebase against all doc and roadmap files to produce the optimization popup content — both structural (workflow-level) and code-level suggestions. Brings external engineering knowledge to evaluate whether the user's chosen approach is actually correct for their use case.

| Property | Value |
|---|---|
| Mode | Dev — Explore Mode (auto-triggered) |
| Model | Nova 2 Lite |
| Input | Full file tree + all .md doc files in repo + FAISS code chunks + `planned_components` list from Codebase Explorer Agent |
| Output | JSON optimization object: `workflow_suggestions` (for updated Mermaid diagram), `code_level_cards` (per-file suggestion cards), `questions` (clarifying questions if docs are ambiguous) |
| Tools Available | Knowledge base retrieval, external engineering knowledge base (Nova Lite's training) |
| Spawned By | Orchestrator (auto, immediately after Codebase Explorer Agent completes diagram) |
| Spawns | None |

**Behavior Notes:**
- Produces two distinct outputs that surface in the UI:
  1. **Optimized Workflow Diagram** — a new Mermaid diagram (not a modification of the original) that may include additional nodes/files Vega recommends adding. Structural changes shown here.
  2. **Code-Level Optimization Cards** — one card per affected file, shown in a separate panel. Each card has the filename top-left, the suggestion, and the rationale.
- **Optimization has two layers:**
  - **Internal** — gaps between what roadmap/docs say and what is actually built. Surfaced as gray→green path suggestions.
  - **External** — Vega's own engineering knowledge applied to the project's goals. E.g.: large dataset + pandas in requirements → suggest Polars. Node.js + no rate limiting middleware → suggest express-rate-limit. ML project with no validation split → flag data leakage risk.
- External suggestions must always include a rationale tied to the specific project context — not generic advice. "Your roadmap mentions 1.5M+ data entries and you're using pandas, which loads the full dataset into RAM. Polars processes data lazily and is 5–20× faster at this scale" — not just "use Polars."
- If docs are ambiguous or contradictory (roadmap says X, code does Y with no explanation), agent generates clarifying questions for the user before producing optimization suggestions.
- Optimization popup is triggered by a UI button with an attention sound — not auto-played. User must click to hear suggestions.
- Agent never modifies files directly. It only produces suggestions. Any actual file changes route through the Code Action Agent in Ops Mode.

**Output Schema:**
```json
{
  "status": "ok | needs_clarification",
  "questions": ["list of clarifying questions if docs are ambiguous"],
  "workflow_suggestions": {
    "has_changes": true,
    "mermaid": "updated flowchart mermaid string with new/modified nodes",
    "changes_summary": "1-2 sentence voice-ready summary of what changed structurally"
  },
  "code_level_cards": [
    {
      "file": "requirements.txt",
      "current_approach": "pandas for data processing",
      "suggested_approach": "Polars for large dataset processing",
      "rationale": "Your dataset exceeds 1M entries. Pandas loads data fully into RAM; Polars uses lazy evaluation and is significantly faster at this scale.",
      "effort": "low | medium | high"
    }
  ]
}
```

---

## 🆕 Code Action Agent

> [!NOTE] Role
> The execution agent for all physical file-level changes — creating new files, modifying existing code, refactoring. Lives in Ops Mode because it performs real actions, not analysis. Triggered whenever Dev Mode detects that a user's request requires writing or modifying code rather than just reviewing it. The UI visually switches to Ops Mode (color change) when this agent is active.

| Property | Value |
|---|---|
| Mode | **Ops — Code Action Pipeline** (separate from AWS Incident Pipeline) |
| Model | Nova 2 Lite |
| Input | User's voice request (what to build/change) + relevant FAISS code chunks + file tree + root cause object (if triggered from incident fix) |
| Output | JSON action object: `action_type`, `target_file`, `proposed_change` (unified diff), `explanation` (voice-ready), `confidence_score`, `warnings` |
| Tools Available | GitHub API (create/modify files, open PRs) — requires voice confirmation before executing |
| Spawned By | Orchestrator (triggered by Dev→Ops auto-switch when user requests file changes) |
| Spawns | None |

**Behavior Notes:**
- **Dev→Ops auto-switch trigger conditions:**
  - User says "add a file", "create a new", "write the code for", "change this function", "refactor this", "fix this bug" → Orchestrator switches to Ops, spawns Code Action Agent
  - UI reflects the switch: panel border color changes from blue (Dev) to red (Ops), mode badge updates in real time
  - Switch back to Dev Mode happens automatically after action completes or is cancelled
- **Build Mode behavior** (when user is in Build Mode):
  1. First describes what needs to be built in plain English (voice response)
  2. Then identifies the specific file(s) to create or modify
  3. Then proposes the code change and asks for voice confirmation before writing
- Response format follows two-step pattern:
  - Step 1 (voice): "The next component to build based on your roadmap is the authentication middleware. It should live in `middleware/auth.py` and handle JWT validation."
  - Step 2 (voice + UI): "Here's what I'll write. Should I create this file?"
- All file writes require explicit voice confirmation — same safety gate as AWS actions.
- If confidence_score < 0.6, agent states this before asking for confirmation: "My confidence in this implementation is moderate — I'd recommend reviewing it before merging."
- Never rewrites more than the minimum needed. Surgical changes only — no opportunistic refactors.

**Output Schema:**
```json
{
  "status": "ok | cannot_generate",
  "action_type": "create_file | modify_file | refactor",
  "target_file": "path/to/file.py",
  "proposed_change": "unified diff string",
  "explanation": "Plain English, voice-ready, max 3 sentences",
  "confidence_score": 0.0,
  "warnings": [
    {
      "type": "side_effect | dependency | breaking_change",
      "description": "Specific risk the developer should know"
    }
  ],
  "proposed_pr_title": "Short GitHub PR title",
  "proposed_pr_body": "Markdown PR description"
}
```

---

## 🔄 Updated: Orchestrator Agent — Mode System

The Orchestrator now manages **three Vega sub-modes within Dev Mode** and handles **auto-switching between Dev and Ops**:

### Dev Mode Sub-Modes

| Sub-Mode | Trigger | Agents Spawned |
|---|---|---|
| **Explore Mode** | Auto on session start | Codebase Explorer Agent → Project Intelligence Agent |
| **Review Mode** | "review", "audit", "check", "scan", "find issues" | Security Audit Agent, Code Review Agent, Architecture Analysis Agent |
| **Build Mode** | "help me build", "what should I build next", "walk me through building" | Project Intelligence Agent (for roadmap context) → Code Action Agent (Ops) |

### Dev→Ops Auto-Switch

When the Orchestrator detects **execution intent** in a Dev Mode session, it automatically transitions to Ops Mode and spawns the appropriate Ops pipeline:

| Execution Intent Keywords | Ops Pipeline Triggered |
|---|---|
| "add file", "create", "write the code", "implement", "build this" | Code Action Pipeline → Code Action Agent |
| "fix this", "change this function", "refactor" | Code Action Pipeline → Code Action Agent |
| "my lambda is failing", "prod is down", "check the logs" | AWS Incident Pipeline → Incident Analysis Agent |

- UI receives `mode_switch` event with `from: "dev"` and `to: "ops"` — triggers visual change (blue → red UI)
- Mode switches back to Dev automatically after action completes, is cancelled, or user says "go back"
- Orchestrator updated intent classification to include: `dev_explore`, `dev_review`, `dev_build`, `ops_incident`, `ops_code_action`, `ops_followup`, `ambiguous`
