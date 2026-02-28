# log_parser.py
# Phase 6 — Log Parsing Agent
# Model:   Nova 2 Lite via Bedrock
# Prompt:  prompts/ops_mode/log_parser.txt
# Inputs:  raw_logs (Boto3 CloudWatch response) + incident object + time_window
# Outputs: JSON {status, error_count, warning_count, key_events[], anomaly_patterns[], voice_summary}
# Rules:   extract full stack traces (never truncate), filter noise aggressively,
#          retry with +30min window if empty log set returned
# TODO: CloudWatch log event parsing, anomaly pattern detection, trace extraction
