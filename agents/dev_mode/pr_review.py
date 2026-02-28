# pr_review.py
# Phase 5 — PR Review Agent
# Model:   Nova 2 Lite via Bedrock
# Prompt:  prompts/dev_mode/pr_review.txt
# Inputs:  pr_diff (unified diff) + pr_description + code_chunks + original_query
# Outputs: JSON {status, verdict (approve|request_changes|comment), summary,
#               breaking_changes_detected, inline_comments[], missing_tests}
# Rules:   all 3 verdicts require voice confirmation before GitHub API call
# TODO: diff parsing, breaking change detection, GitHub PR review API call
