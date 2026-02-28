# security_audit.py
# Phase 5 — Security Audit Agent (PRIMARY Dev Mode golden path agent)
# Model:   Nova 2 Lite via Bedrock
# Prompt:  prompts/dev_mode/security_audit.txt
# Inputs:  code_chunks + dependency_files + original_query
# Outputs: JSON {status, vulnerability_count{}, findings[], voice_summary}
# Rules:   hardcoded secrets always CRITICAL, SQL injection always CRITICAL,
#          voice_summary covers CRITICAL+HIGH only
# TODO: OWASP Top 10 scan logic, CVE reference lookup, dependency version check
