# incident.py
# Phase 6 — Incident Analysis Agent (first responder in Ops Mode)
# Model:   Nova 2 Lite via Bedrock
# Prompt:  prompts/ops_mode/incident.txt
# Inputs:  voice_text + session_memory + current_time_utc
# Outputs: JSON {status, clarifying_question, incident{service, time_window,
#               severity P1/P2/P3, description, assumptions[]},
#               next_action="retrieve_logs", voice_acknowledgement}
# Rules:   default time window = last 60 min if not stated
# TODO: NLP extraction of service name + time window, severity classification
