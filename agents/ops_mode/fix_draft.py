# fix_draft.py
# Phase 6 — Fix Draft Agent (end of Ops Mode golden path)
# Model:   Nova 2 Lite via Bedrock
# Prompt:  prompts/ops_mode/fix_draft.txt
# Inputs:  root_cause object + code_chunks + incident object
# Outputs: JSON {status, fix_diff (unified diff), explanation (voice-ready, max 3 sentences),
#               confidence_score (0.0-1.0), files_modified[], warnings[], proposed_pr_title, proposed_pr_body}
# Rules:   confidence_score < 0.6 triggers voice warning,
#          GitHub PR creation requires voice confirmation — agent does NOT call GitHub directly
# TODO: diff generation, side-effect analysis, PR body templating
