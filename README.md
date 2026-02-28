# Vega — Voice-Powered AI Staff Engineer

> Amazon Nova AI Hackathon 2026 | Deadline: March 16, 2026

Vega is a multi-agent AI system that acts as a voice-activated staff engineer for developers.
Voice in → intent classification → specialized agents → voice out → autonomous actions (with confirmation gate).

## Modes
- **Dev Mode** — code review, security audit, architecture analysis, PR review
- **Ops Mode** — incident triage, CloudWatch log parsing, root cause analysis, fix drafting

## Tech Stack
Nova 2 Sonic (voice I/O) · Nova 2 Lite (9 reasoning agents) · Nova Multimodal Embeddings (knowledge base) · Nova Act (UI automation) · FastAPI · FAISS · React

## Setup
1. Copy `.env.example` to `.env` and fill all values
2. `pip install -r requirements.txt`
3. `GET /health` — verify all connections show "connected"

See `ENV.md` for full setup checklist and IAM policy.
