# code_review.py
# Phase 5 — Code Review Agent
# Model:   Nova 2 Lite via Bedrock
# Prompt:  prompts/dev_mode/code_review.txt
# Inputs:  code_chunks [{file, start_line, end_line, content}] + original_query + session_context
# Outputs: JSON {status, findings[], summary, files_reviewed[], complexity_score}
# Severity: CRITICAL | HIGH | MEDIUM | LOW
# TODO: implement chunk analysis, cyclomatic complexity estimate, finding dedup
