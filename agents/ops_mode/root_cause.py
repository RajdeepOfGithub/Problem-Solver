# root_cause.py
# Phase 6 — Root Cause Agent
# Model:   Nova 2 Lite via Bedrock
# Prompt:  prompts/ops_mode/root_cause.txt
# Inputs:  log_summary (from Log Parser) + code_chunks + incident object
# Outputs: JSON {status, root_cause_statement, confidence (high|medium|low),
#               evidence[], suspected_commit, next_action="generate_fix", confidence_rationale}
# Rules:   confidence "high" only when log stack trace maps directly to code line,
#          root_cause_statement is voice-ready plain English (no code/markdown)
# TODO: log-to-code correlation logic, git blame integration
