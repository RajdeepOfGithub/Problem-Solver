"use client";
import { useState, useEffect, useRef, useCallback } from "react";
import VegaDiagram from "./components/VegaDiagram";
import ModeIndicator from "./components/ModeIndicator";

// ── Constants ──────────────────────────────────────────────────────────────
const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const WS_BASE = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000";

const SEVERITY_COLOR = {
  CRITICAL: "#ff3b30",
  HIGH: "#ff9f0a",
  MEDIUM: "#ffd60a",
  LOW: "#30d158",
};

const MODE_CONFIG = {
  dev: { label: "Dev Mode", color: "#0a84ff", icon: "⬡", desc: "Code Review · Security · Architecture · PR" },
  ops: { label: "Ops Mode", color: "#ff453a", icon: "◈", desc: "Incident · Logs · Root Cause · Fix Draft" },
};

const AWS_SERVICES = [
  { id: "bedrock", label: "Bedrock" },
  { id: "cloudwatch", label: "CloudWatch" },
  { id: "lambda", label: "Lambda" },
  { id: "ecs", label: "ECS" },
];

// ── Helpers ────────────────────────────────────────────────────────────────
function formatTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}
function formatAgo(iso) {
  if (!iso) return "—";
  const s = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}
function base64ToBlob(b64, type = "audio/pcm") {
  const bin = atob(b64);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return new Blob([buf], { type });
}

// ── Web Audio for mic capture (16kHz mono PCM) ─────────────────────────────
async function startMicCapture(onChunk) {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true } });
  const ctx = new AudioContext({ sampleRate: 16000 });
  const source = ctx.createMediaStreamSource(stream);
  await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([`
    class PcmProcessor extends AudioWorkletProcessor {
      process(inputs) {
        const ch = inputs[0][0];
        if (ch) this.port.postMessage(ch.slice());
        return true;
      }
    }
    registerProcessor('pcm-proc', PcmProcessor);
  `], { type: "application/javascript" })));
  const node = new AudioWorkletNode(ctx, "pcm-proc");
  node.port.onmessage = (e) => {
    const f32 = e.data;
    const i16 = new Int16Array(f32.length);
    for (let i = 0; i < f32.length; i++) i16[i] = Math.max(-32768, Math.min(32767, f32[i] * 32768));
    onChunk(i16.buffer);
  };
  source.connect(node);
  return { stop: () => { stream.getTracks().forEach(t => t.stop()); ctx.close(); } };
}

// ── Playback base64 audio ─────────────────────────────────────────────────
async function playBase64Audio(b64, actx) {
  try {
    const buf = Uint8Array.from(atob(b64), c => c.charCodeAt(0)).buffer;
    const decoded = await actx.decodeAudioData(buf);
    const src = actx.createBufferSource();
    src.buffer = decoded;
    src.connect(actx.destination);
    src.start();
  } catch (_) {}
}

// ── CSS ────────────────────────────────────────────────────────────────────
const css = `
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600&family=Space+Grotesk:wght@400;500;600&display=swap');

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg0: #0a0a0b;
    --bg1: #111114;
    --bg2: #18181d;
    --bg3: #1f1f26;
    --border: rgba(255,255,255,0.07);
    --border2: rgba(255,255,255,0.12);
    --text0: #f0f0f5;
    --text1: #9898a8;
    --text2: #5a5a6e;
    --cyan: #00e5c8;
    --blue: #0a84ff;
    --red: #ff453a;
    --amber: #ff9f0a;
    --green: #30d158;
    --purple: #bf5af2;
    --mono: 'JetBrains Mono', monospace;
    --sans: 'Space Grotesk', sans-serif;
  }

  body { background: var(--bg0); color: var(--text0); font-family: var(--sans); font-size: 14px; line-height: 1.5; overflow: hidden; }

  /* layout */
  .shell { display: flex; height: 100vh; width: 100vw; overflow: hidden; }
  .sidebar { width: 220px; flex-shrink: 0; background: var(--bg1); border-right: 1px solid var(--border); display: flex; flex-direction: column; overflow: hidden; }
  .center { flex: 1; display: flex; flex-direction: column; overflow: hidden; min-width: 0; }
  .right { width: 280px; flex-shrink: 0; background: var(--bg1); border-left: 1px solid var(--border); display: flex; flex-direction: column; overflow: hidden; }

  /* sidebar */
  .logo { padding: 18px 16px 14px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 10px; }
  .logo-mark { width: 28px; height: 28px; border-radius: 8px; background: linear-gradient(135deg, var(--cyan), #0066cc); display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 700; color: #000; font-family: var(--mono); flex-shrink: 0; box-shadow: 0 0 12px rgba(0,229,200,0.25); }
  .logo-text { font-weight: 600; font-size: 15px; letter-spacing: 0.5px; }
  .logo-sub { font-size: 10px; color: var(--text2); font-family: var(--mono); letter-spacing: 0.5px; }

  .sb-section { padding: 10px 0; border-bottom: 1px solid var(--border); }
  .sb-label { font-size: 10px; font-weight: 600; color: var(--text2); letter-spacing: 1.2px; text-transform: uppercase; padding: 4px 16px 8px; font-family: var(--mono); }

  .repo-card { margin: 6px 10px; background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; cursor: pointer; transition: border-color .15s; }
  .repo-card:hover { border-color: var(--border2); }
  .repo-name { font-size: 12px; font-weight: 500; color: var(--text0); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .repo-meta { font-size: 10px; color: var(--text2); font-family: var(--mono); margin-top: 4px; }
  .repo-status { display: flex; align-items: center; gap: 5px; margin-top: 6px; }
  .dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
  .dot-green { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .dot-amber { background: var(--amber); box-shadow: 0 0 6px var(--amber); animation: blink 1s infinite; }
  .dot-grey { background: var(--text2); }

  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.3} }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes pulse-ring { 0%{box-shadow:0 0 0 0 rgba(0,229,200,.4)} 70%{box-shadow:0 0 0 16px rgba(0,229,200,0)} 100%{box-shadow:0 0 0 0 rgba(0,229,200,0)} }
  @keyframes fade-up { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:none} }
  @keyframes shimmer { 0%{background-position:-200% 0} 100%{background-position:200% 0} }

  .index-btn { margin: 8px 10px 4px; background: rgba(0,229,200,0.1); border: 1px solid rgba(0,229,200,0.25); color: var(--cyan); border-radius: 7px; padding: 7px 12px; font-size: 12px; font-weight: 500; cursor: pointer; width: calc(100% - 20px); text-align: center; transition: all .15s; font-family: var(--sans); }
  .index-btn:hover { background: rgba(0,229,200,0.2); }

  .session-item { padding: 7px 16px; cursor: pointer; transition: background .1s; display: flex; align-items: center; gap: 8px; }
  .session-item:hover { background: var(--bg2); }
  .sess-mode-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
  .sess-label { font-size: 12px; color: var(--text1); flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sess-time { font-size: 10px; color: var(--text2); font-family: var(--mono); flex-shrink: 0; }

  .aws-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; padding: 4px 10px 8px; }
  .aws-chip { background: var(--bg2); border: 1px solid var(--border); border-radius: 6px; padding: 5px 8px; display: flex; align-items: center; gap: 5px; font-size: 10px; font-family: var(--mono); color: var(--text1); }
  .sb-bottom { margin-top: auto; padding: 10px; }

  /* center: mode banner */
  .mode-banner { padding: 10px 20px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; background: var(--bg1); flex-shrink: 0; }
  .mode-badge { display: flex; align-items: center; gap: 8px; }
  .mode-icon { font-size: 16px; }
  .mode-name { font-weight: 600; font-size: 13px; }
  .mode-sub { font-size: 11px; color: var(--text2); font-family: var(--mono); }
  .mode-toggle { display: flex; gap: 6px; }
  .mode-btn { padding: 5px 12px; border-radius: 6px; border: 1px solid var(--border2); background: transparent; color: var(--text1); font-size: 12px; cursor: pointer; transition: all .15s; font-family: var(--sans); }
  .mode-btn.active { background: rgba(10,132,255,0.15); border-color: var(--blue); color: var(--blue); }
  .mode-btn.ops.active { background: rgba(255,69,58,0.15); border-color: var(--red); color: var(--red); }

  /* transcript */
  .transcript-area { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 14px; }
  .transcript-area::-webkit-scrollbar { width: 4px; }
  .transcript-area::-webkit-scrollbar-track { background: transparent; }
  .transcript-area::-webkit-scrollbar-thumb { background: var(--bg3); border-radius: 4px; }

  .msg { display: flex; gap: 10px; animation: fade-up .2s ease; }
  .msg.user { flex-direction: row-reverse; }
  .msg-avatar { width: 28px; height: 28px; border-radius: 8px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 700; }
  .avatar-vega { background: linear-gradient(135deg, var(--cyan), #0066cc); color: #000; }
  .avatar-user { background: var(--bg3); border: 1px solid var(--border2); color: var(--text1); }
  .msg-bubble { max-width: 68%; }
  .msg.user .msg-bubble { align-items: flex-end; }
  .bubble-text { background: var(--bg2); border: 1px solid var(--border); border-radius: 10px; padding: 10px 14px; font-size: 13px; line-height: 1.6; }
  .msg.user .bubble-text { background: rgba(10,132,255,0.12); border-color: rgba(10,132,255,0.25); }
  .bubble-time { font-size: 10px; color: var(--text2); font-family: var(--mono); margin-top: 4px; padding: 0 4px; }
  .msg.user .bubble-time { text-align: right; }
  .partial-text { color: var(--text1); font-style: italic; }

  .finding-pill { display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px; border-radius: 4px; font-size: 10px; font-family: var(--mono); font-weight: 600; margin: 2px; }

  .vega-thinking { display: flex; gap: 10px; align-items: flex-start; }
  .thinking-dots { display: flex; gap: 4px; align-items: center; padding: 12px 14px; background: var(--bg2); border: 1px solid var(--border); border-radius: 10px; }
  .thinking-dots span { width: 5px; height: 5px; border-radius: 50%; background: var(--cyan); opacity: .3; animation: thinking 1.2s infinite; }
  .thinking-dots span:nth-child(2) { animation-delay: .2s; }
  .thinking-dots span:nth-child(3) { animation-delay: .4s; }
  @keyframes thinking { 0%,100%{opacity:.3;transform:scale(1)} 50%{opacity:1;transform:scale(1.3)} }

  /* landing */
  .landing { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 24px; padding: 40px; }
  .landing-logo { width: 64px; height: 64px; border-radius: 18px; background: linear-gradient(135deg, var(--cyan), #0066cc); display: flex; align-items: center; justify-content: center; font-size: 28px; font-weight: 700; color: #000; font-family: var(--mono); box-shadow: 0 0 40px rgba(0,229,200,0.2); }
  .landing-title { font-size: 24px; font-weight: 600; letter-spacing: -0.3px; }
  .landing-sub { font-size: 13px; color: var(--text1); text-align: center; max-width: 380px; line-height: 1.7; }
  .landing-form { width: 100%; max-width: 440px; display: flex; flex-direction: column; gap: 10px; }
  .form-label { font-size: 11px; font-weight: 500; color: var(--text2); font-family: var(--mono); letter-spacing: 0.8px; text-transform: uppercase; }
  .form-input { background: var(--bg2); border: 1px solid var(--border2); border-radius: 8px; padding: 10px 14px; color: var(--text0); font-family: var(--mono); font-size: 12px; outline: none; transition: border-color .15s; }
  .form-input:focus { border-color: var(--cyan); }
  .form-input::placeholder { color: var(--text2); }
  .submit-btn { background: linear-gradient(135deg, var(--cyan), #0066cc); color: #000; font-weight: 600; font-size: 13px; border: none; border-radius: 8px; padding: 11px; cursor: pointer; transition: opacity .15s; font-family: var(--sans); }
  .submit-btn:hover { opacity: .88; }
  .submit-btn:disabled { opacity: .4; cursor: default; }

  /* indexing */
  .indexing-panel { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 20px; padding: 40px; }
  .progress-box { width: 100%; max-width: 420px; background: var(--bg2); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }
  .progress-title { font-size: 13px; font-weight: 500; margin-bottom: 4px; }
  .progress-sub { font-size: 11px; color: var(--text2); font-family: var(--mono); margin-bottom: 14px; }
  .progress-bar-bg { background: var(--bg3); border-radius: 4px; height: 4px; overflow: hidden; }
  .progress-bar-fill { height: 100%; border-radius: 4px; background: linear-gradient(90deg, var(--cyan), #0066cc); transition: width .3s ease; background-size: 200% 100%; }
  .progress-stats { display: flex; justify-content: space-between; margin-top: 10px; font-size: 10px; font-family: var(--mono); color: var(--text2); }
  .spinner { width: 16px; height: 16px; border: 2px solid var(--border2); border-top-color: var(--cyan); border-radius: 50%; animation: spin .7s linear infinite; }

  /* diagram */
  .diagram-panel { flex: 1; overflow: auto; padding: 20px; }

  /* mic area */
  .mic-area { padding: 16px 20px; border-top: 1px solid var(--border); display: flex; align-items: center; gap: 16px; background: var(--bg1); flex-shrink: 0; }
  .mic-btn { width: 52px; height: 52px; border-radius: 50%; border: none; cursor: pointer; display: flex; align-items: center; justify-content: center; flex-shrink: 0; font-size: 20px; transition: all .2s; position: relative; }
  .mic-btn.idle { background: var(--bg2); border: 2px solid var(--border2); }
  .mic-btn.idle:hover { border-color: var(--cyan); }
  .mic-btn.listening { background: rgba(0,229,200,0.15); border: 2px solid var(--cyan); animation: pulse-ring 1.5s infinite; }
  .mic-btn.processing { background: rgba(10,132,255,0.15); border: 2px solid var(--blue); }
  .mic-label { font-size: 11px; color: var(--text2); font-family: var(--mono); }
  .waveform { flex: 1; height: 36px; display: flex; align-items: center; gap: 2px; overflow: hidden; }
  .wave-bar { width: 3px; border-radius: 2px; background: var(--cyan); opacity: .5; transition: height .05s; min-height: 3px; }

  /* bottom bar */
  .status-bar { padding: 6px 16px; border-top: 1px solid var(--border); display: flex; align-items: center; gap: 16px; background: var(--bg0); flex-shrink: 0; }
  .stat-chip { display: flex; align-items: center; gap: 5px; font-size: 10px; font-family: var(--mono); color: var(--text2); }
  .agent-pills { display: flex; gap: 4px; flex: 1; justify-content: center; }
  .agent-pill { padding: 2px 8px; border-radius: 20px; font-size: 10px; font-family: var(--mono); border: 1px solid; }
  .pill-active { border-color: rgba(0,229,200,0.4); color: var(--cyan); background: rgba(0,229,200,0.08); }
  .pill-idle { border-color: var(--border); color: var(--text2); }

  /* right panel */
  .right-header { padding: 14px 16px 10px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; }
  .right-title { font-size: 12px; font-weight: 600; letter-spacing: 0.3px; }
  .right-body { flex: 1; overflow-y: auto; padding: 10px; display: flex; flex-direction: column; gap: 6px; }
  .right-body::-webkit-scrollbar { width: 3px; }
  .right-body::-webkit-scrollbar-thumb { background: var(--bg3); }

  .action-item { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; padding: 9px 11px; animation: fade-up .2s; }
  .action-row { display: flex; align-items: flex-start; gap: 7px; }
  .action-icon { font-size: 13px; flex-shrink: 0; margin-top: 1px; }
  .action-desc { font-size: 11px; color: var(--text1); line-height: 1.5; flex: 1; }
  .action-meta { display: flex; align-items: center; justify-content: space-between; margin-top: 5px; }
  .action-status { font-size: 10px; font-family: var(--mono); font-weight: 600; }
  .action-time { font-size: 10px; color: var(--text2); font-family: var(--mono); }
  .status-success { color: var(--green); }
  .status-pending { color: var(--amber); }
  .status-failed { color: var(--red); }
  .status-cancelled { color: var(--text2); }
  .status-executing { color: var(--blue); }
  .status-awaiting { color: var(--cyan); }

  .summary-cards { padding: 8px 10px; border-top: 1px solid var(--border); display: flex; gap: 6px; flex-shrink: 0; }
  .summary-card { flex: 1; background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; }
  .sc-num { font-size: 20px; font-weight: 600; font-family: var(--mono); }
  .sc-label { font-size: 10px; color: var(--text2); margin-top: 2px; }

  /* confirmation modal */
  .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.7); display: flex; align-items: center; justify-content: center; z-index: 100; backdrop-filter: blur(4px); }
  .modal { background: var(--bg2); border: 1px solid var(--border2); border-radius: 14px; padding: 24px; max-width: 400px; width: 90%; box-shadow: 0 20px 60px rgba(0,0,0,0.5); animation: fade-up .2s; }
  .modal-icon { font-size: 28px; margin-bottom: 12px; }
  .modal-title { font-size: 15px; font-weight: 600; margin-bottom: 6px; }
  .modal-body { font-size: 13px; color: var(--text1); line-height: 1.6; margin-bottom: 18px; }
  .modal-btns { display: flex; gap: 8px; }
  .modal-confirm { flex: 1; background: rgba(0,229,200,0.15); border: 1px solid var(--cyan); color: var(--cyan); font-weight: 600; font-size: 13px; border-radius: 8px; padding: 10px; cursor: pointer; transition: all .15s; font-family: var(--sans); }
  .modal-confirm:hover { background: rgba(0,229,200,0.25); }
  .modal-reject { flex: 1; background: rgba(255,69,58,0.1); border: 1px solid rgba(255,69,58,0.3); color: var(--red); font-weight: 500; font-size: 13px; border-radius: 8px; padding: 10px; cursor: pointer; transition: all .15s; font-family: var(--sans); }
  .modal-reject:hover { background: rgba(255,69,58,0.2); }

  /* intelligence cards */
  .intel-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 10px; }
  .intel-card { background: var(--bg2); border: 1px solid var(--border); border-radius: 10px; padding: 12px; }
  .intel-badge { font-size: 10px; font-family: var(--mono); font-weight: 600; color: var(--amber); margin-bottom: 6px; }
  .intel-title { font-size: 12px; font-weight: 500; margin-bottom: 4px; }
  .intel-body { font-size: 11px; color: var(--text1); line-height: 1.5; }

  /* index modal */
  .index-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.7); display: flex; align-items: center; justify-content: center; z-index: 100; backdrop-filter: blur(4px); }
  .index-modal-box { background: var(--bg2); border: 1px solid var(--border2); border-radius: 14px; padding: 24px; max-width: 380px; width: 90%; animation: fade-up .2s; }

  .tab-bar { display: flex; border-bottom: 1px solid var(--border); flex-shrink: 0; }
  .tab { padding: 10px 16px; font-size: 12px; color: var(--text2); cursor: pointer; border-bottom: 2px solid transparent; transition: all .15s; font-weight: 500; }
  .tab.active { color: var(--cyan); border-bottom-color: var(--cyan); }

  .diagram-wrap { background: var(--bg1); border-radius: 10px; border: 1px solid var(--border); padding: 16px; min-height: 200px; display: flex; align-items: center; justify-content: center; }
  .diagram-placeholder { color: var(--text2); font-family: var(--mono); font-size: 12px; text-align: center; }

  /* skeleton */
  .skeleton { background: linear-gradient(90deg, var(--bg2) 25%, var(--bg3) 50%, var(--bg2) 75%); background-size: 200% 100%; animation: shimmer 1.5s infinite; border-radius: 6px; }

  .empty-state { flex: 1; display: flex; align-items: center; justify-content: center; color: var(--text2); font-family: var(--mono); font-size: 12px; flex-direction: column; gap: 8px; }

  .repo-url-short { font-family: var(--mono); font-size: 11px; color: var(--text2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }

  @keyframes modeTransitionFlash {
    0%   { opacity: 0; }
    20%  { opacity: 1; }
    100% { opacity: 0; }
  }
  .mode-transition-flash {
    animation: modeTransitionFlash 600ms ease forwards;
    background: linear-gradient(90deg, rgba(99,102,241,0.12) 0%, rgba(220,38,38,0.12) 100%);
  }

`;

// ── Action icon helper ────────────────────────────────────────────────────
function actionIcon(type, status) {
  if (status === "success") return "✅";
  if (status === "failed") return "❌";
  if (status === "cancelled") return "⛔";
  if (status === "executing") return "⚡";
  if (status === "awaiting_confirmation") return "🔔";
  const icons = { github_issue: "📝", github_pr: "🔀", github_review: "🔍", aws_read: "📊", aws_write: "✏️" };
  return icons[type] || "•";
}

// ── Main App ──────────────────────────────────────────────────────────────
export default function VegaUI() {
  // State
  const [screen, setScreen] = useState("landing"); // landing | indexing | ready | session
  const [mode, setMode] = useState("dev");
  const [repoUrl, setRepoUrl] = useState("");
  const [branch, setBranch] = useState("main");
  const [githubToken, setGithubToken] = useState("");
  const [jobId, setJobId] = useState(null);
  const [indexProgress, setIndexProgress] = useState(0);
  const [indexChunks, setIndexChunks] = useState(0);
  const [indexTotal, setIndexTotal] = useState(1);
  const [indexFileCount, setIndexFileCount] = useState(0);
  const [indexedRepo, setIndexedRepo] = useState(null);
  const [health, setHealth] = useState({});
  const [diagramData, setDiagramData] = useState({ nodes: [], edges: [] });
  const [buildStatusData, setBuildStatusData] = useState({});
  const [viewMode, setViewMode] = useState("architecture");
  const [sessionId, setSessionId] = useState(null);
  const [sessionToken, setSessionToken] = useState(null);
  const [messages, setMessages] = useState([]);
  const [actions, setActions] = useState([]);
  const [micState, setMicState] = useState("idle"); // idle | listening | processing
  const [partialTranscript, setPartialTranscript] = useState("");
  const [confirmation, setConfirmation] = useState(null);
  const [activeAgents, setActiveAgents] = useState([]);
  const [waveData, setWaveData] = useState(new Array(40).fill(4));
  const [latency, setLatency] = useState(null);
  const [activeTab, setActiveTab] = useState("diagram");
  const [showIndexModal, setShowIndexModal] = useState(false);
  const [recentSessions, setRecentSessions] = useState([]);
  const [isIndexing, setIsIndexing] = useState(false);
  const [intelCards, setIntelCards] = useState([]);
  const [highlightedNodes, setHighlightedNodes] = useState([]);
  const [currentMode, setCurrentMode] = useState("dev_explore");
  const [modeFamily, setModeFamily] = useState("dev");
  const [isTransitioning, setIsTransitioning] = useState(false);
  const [isRecording, setIsRecording] = useState(false);

  // Refs
  const wsRef = useRef(null);
  const micRef = useRef(null);
  const audioCtxRef = useRef(null);
  const pollRef = useRef(null);
  const transcriptEndRef = useRef(null);
  const waveAnim = useRef(null);
  const modeFamilyRef = useRef("dev");
  const transitionTimeoutRef = useRef(null);

  // Scroll transcript to bottom
  useEffect(() => { transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, partialTranscript]);

  // Health polling
  useEffect(() => {
    const checkHealth = async () => {
      try {
        const r = await fetch(`${API}/health`);
        if (r.ok) setHealth(await r.json());
      } catch (_) {}
    };
    checkHealth();
    const t = setInterval(checkHealth, 15000);
    return () => clearInterval(t);
  }, []);

  // ── Index repo ─────────────────────────────────────────────────────────
  async function indexRepo() {
    if (!repoUrl.trim()) return;
    setIsIndexing(true);
    setShowIndexModal(false);
    try {
      const body = { repo_url: repoUrl.trim(), branch: branch.trim() || "main" };
      if (githubToken.trim()) body.github_token = githubToken.trim();
      const r = await fetch(`${API}/repo/index`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const data = await r.json();
      if (r.ok) {
        setJobId(data.job_id);
        setIndexTotal(data.estimated_duration_seconds || 60);
        setIndexedRepo({ url: repoUrl.trim(), branch: branch.trim() || "main", jobId: data.job_id });
        setScreen("indexing");
        pollStatus(data.job_id);
      } else {
        alert("Index failed: " + (data.detail || JSON.stringify(data)));
      }
    } catch (e) {
      alert("Network error: " + e.message);
    }
    setIsIndexing(false);
  }

  function pollStatus(jid) {
    clearInterval(pollRef.current);
    const t0 = Date.now();
    pollRef.current = setInterval(async () => {
      try {
        const r = await fetch(`${API}/repo/status/${jid}`);
        if (!r.ok) return;
        const d = await r.json();
        setIndexProgress(d.progress || 0);
        setIndexChunks(d.chunks_indexed || 0);
        setIndexTotal(d.total_chunks || 1);
        setIndexFileCount(d.file_count || 0);
        if (d.status === "complete") {
          clearInterval(pollRef.current);
          setLatency(Date.now() - t0);
          fetchDiagram(jid);
        } else if (d.status === "failed") {
          clearInterval(pollRef.current);
          alert("Indexing failed: " + (d.error || "unknown error"));
          setScreen("landing");
        }
      } catch (_) {}
    }, 1500);
  }

  async function fetchDiagram(jid) {
    try {
      const r = await fetch(`${API}/repo/diagram/${jid}`);
      if (r.ok) {
        const d = await r.json();
        setDiagramData({ nodes: d.nodes || [], edges: d.edges || [] });
      }
      // Also fetch two-tone to populate build-status view
      try {
        const r2 = await fetch(`${API}/repo/diagram/${jid}/two-tone`);
        if (r2.ok) {
          const d2 = await r2.json();
          const bsMap = {};
          Object.entries(d2.styles_applied || {}).forEach(([k, v]) => {
            bsMap[k] = v === "built" ? "complete" : v === "stub" ? "planned" : "research";
          });
          setBuildStatusData(bsMap);
        }
      } catch (_) {}
    } catch (_) {}
    setScreen("ready");
  }

  // ── Start session ──────────────────────────────────────────────────────
  async function startSession(m) {
    if (!indexedRepo?.jobId) return;
    try {
      const r = await fetch(`${API}/session/start`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: m, repo_id: indexedRepo.jobId }) });
      const d = await r.json();
      if (r.ok) {
        setSessionId(d.session_id);
        setSessionToken(d.token);
        setMode(m);
        setMessages([]);
        setActions([]);
        setScreen("session");
        // Reset mode indicator to dev_explore at session start
        setCurrentMode("dev_explore");
        setModeFamily("dev");
        setIsTransitioning(false);
        modeFamilyRef.current = "dev";
        setRecentSessions(prev => [{ id: d.session_id, mode: m, time: new Date().toISOString() }, ...prev.slice(0, 9)]);
        connectWS(d.session_id, d.token);
      }
    } catch (e) {
      alert("Failed to start session: " + e.message);
    }
  }

  // ── WebSocket ──────────────────────────────────────────────────────────
  function connectWS(sid, tok) {
    if (wsRef.current) wsRef.current.close();
    const url = `${WS_BASE}/ws/voice?token=${encodeURIComponent(tok)}`;
    const ws = new WebSocket(url);
    wsRef.current = ws;
    ws.binaryType = "arraybuffer";

    ws.onopen = () => console.log("[WS] connected");
    ws.onerror = (e) => console.error("[WS] error", e);
    ws.onclose = () => { setMicState("idle"); stopMic(); };

    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        const frame = JSON.parse(ev.data);
        handleFrame(frame);
      }
    };
  }

  function handleFrame(frame) {
    switch (frame.type) {
      case "transcript":
        if (frame.is_final) {
          setPartialTranscript("");
          setMessages(prev => [...prev, { role: "user", content: frame.text, ts: new Date().toISOString() }]);
          setMicState("processing");
          setActiveAgents(["Orchestrator"]);
        } else {
          setPartialTranscript(frame.text);
        }
        break;

      case "action_update":
        setActions(prev => {
          const idx = prev.findIndex(a => a.action_id === frame.action_id);
          const item = { action_id: frame.action_id, description: frame.description, status: frame.status, timestamp: new Date().toISOString(), type: frame.action_type || "github_issue" };
          if (idx >= 0) { const a = [...prev]; a[idx] = { ...a[idx], ...item }; return a; }
          return [item, ...prev];
        });
        setActiveAgents(prev => {
          const agent = frame.agent || "Agent";
          return prev.includes(agent) ? prev : [...prev, agent];
        });
        break;

      case "response_audio":
        if (frame.highlighted_nodes?.length) setHighlightedNodes(frame.highlighted_nodes);
        if (frame.chunk && audioCtxRef.current) playBase64Audio(frame.chunk, audioCtxRef.current);
        if (frame.is_final) {
          setIsRecording(false);
          setMicState("idle");
          setActiveAgents([]);
          if (frame.text) {
            setMessages(prev => [...prev, { role: "vega", content: frame.text, ts: new Date().toISOString() }]);
          }
        }
        break;

      case "confirmation_required":
        setConfirmation({ action_id: frame.action_id, prompt: frame.prompt });
        break;

      case "mode_change": {
        const newFamily = frame.mode_family;
        const prevFamily = modeFamilyRef.current;
        modeFamilyRef.current = newFamily;
        setCurrentMode(frame.intent);
        setModeFamily(newFamily);
        // Trigger transition flash on dev→ops crossover
        if (prevFamily === "dev" && newFamily === "ops") {
          setIsTransitioning(true);
          clearTimeout(transitionTimeoutRef.current);
          transitionTimeoutRef.current = setTimeout(() => setIsTransitioning(false), 600);
        }
        break;
      }

      case "error":
        console.error("[WS] error frame:", frame);
        setMicState("idle");
        setMessages(prev => [...prev, { role: "vega", content: `⚠ ${frame.message}`, ts: new Date().toISOString(), isError: true }]);
        break;
    }
  }

  // ── Mic ───────────────────────────────────────────────────────────────
  async function toggleRecording() {
    if (isRecording) {
      // STOP path
      stopMic();
      if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(new ArrayBuffer(0));
      setIsRecording(false);
      setMicState("processing");
      stopWaveAnim();
      return;
    }
    // START path
    if (micState !== "idle") return;
    try {
      if (!audioCtxRef.current) audioCtxRef.current = new AudioContext();
      micRef.current = await startMicCapture((chunk) => {
        if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(chunk);
      });
      setIsRecording(true);
      setMicState("listening");
      startWaveAnim();
    } catch (e) {
      alert("Mic error: " + e.message);
    }
  }

  function stopMic() {
    micRef.current?.stop();
    micRef.current = null;
  }

  function startWaveAnim() {
    stopWaveAnim();
    waveAnim.current = setInterval(() => {
      setWaveData(Array.from({ length: 40 }, () => Math.random() * 28 + 4));
    }, 80);
  }
  function stopWaveAnim() {
    clearInterval(waveAnim.current);
    setWaveData(new Array(40).fill(4));
  }

  // ── Confirmation ──────────────────────────────────────────────────────
  async function handleConfirm(confirmed) {
    if (!confirmation || !sessionId) return;
    try {
      await fetch(`${API}/action/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${sessionToken}` },
        body: JSON.stringify({ session_id: sessionId, action_id: confirmation.action_id, confirmed }),
      });
    } catch (_) {}
    setConfirmation(null);
  }

  // ── Project Intelligence ──────────────────────────────────────────────
  async function loadIntelligence() {
    if (!indexedRepo?.jobId) return;
    try {
      const r = await fetch(`${API}/session/optimize`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo_id: indexedRepo.jobId }) });
      if (r.ok) {
        const d = await r.json();
        setIntelCards(d.suggestions || d.cards || []);
        setActiveTab("intelligence");
      }
    } catch (_) {}
  }

  // ── fetch session history when entering session screen ────────────────
  useEffect(() => {
    if (screen !== "session" || !sessionId || !sessionToken) return;
    const fetchHistory = async () => {
      try {
        const r = await fetch(`${API}/session/${sessionId}/history`, { headers: { Authorization: `Bearer ${sessionToken}` } });
        if (r.ok) {
          const d = await r.json();
          if (d.messages?.length) setMessages(d.messages.map(m => ({ role: m.role === "vega" ? "vega" : "user", content: m.content, ts: m.timestamp })));
        }
      } catch (_) {}
    };
    fetchHistory();
  }, [screen, sessionId]);

  // Cleanup
  useEffect(() => () => { clearInterval(pollRef.current); wsRef.current?.close(); stopMic(); stopWaveAnim(); clearTimeout(transitionTimeoutRef.current); }, []);

  // ── Render helpers ────────────────────────────────────────────────────
  const healthDot = (key) => {
    const s = health[key];
    if (s === "connected") return "dot-green";
    if (s === "degraded") return "dot-amber";
    return "dot-grey";
  };

  const repoShort = indexedRepo?.url?.replace("https://github.com/", "").replace(/\/$/, "") || "";

  // ── Sidebar ───────────────────────────────────────────────────────────
  const Sidebar = () => (
    <aside className="sidebar">
      <div className="logo">
        <div className="logo-mark">V</div>
        <div>
          <div className="logo-text">Vega</div>
          <div className="logo-sub">AI STAFF ENG</div>
        </div>
      </div>

      <div className="sb-section">
        <div className="sb-label">Repository</div>
        {indexedRepo ? (
          <div className="repo-card">
            <div className="repo-name">{repoShort || "indexed repo"}</div>
            <div className="repo-meta">{indexedRepo.branch} · {indexFileCount || "?"} files</div>
            <div className="repo-status">
              <div className="dot dot-green" />
              <span style={{ fontSize: 10, color: "var(--text2)", fontFamily: "var(--mono)" }}>indexed</span>
            </div>
          </div>
        ) : (
          <div style={{ padding: "4px 10px 6px" }}>
            <div style={{ fontSize: 11, color: "var(--text2)", fontFamily: "var(--mono)", padding: "6px 6px" }}>No repo indexed</div>
          </div>
        )}
        <button className="index-btn" onClick={() => setShowIndexModal(true)}>+ Index Repository</button>
      </div>

      {recentSessions.length > 0 && (
        <div className="sb-section" style={{ flex: 1, overflowY: "auto" }}>
          <div className="sb-label">Recent Sessions</div>
          {recentSessions.map(s => (
            <div key={s.id} className="session-item">
              <div className="sess-mode-dot" style={{ background: s.mode === "dev" ? "var(--blue)" : "var(--red)" }} />
              <span className="sess-label">{s.mode === "dev" ? "Dev" : "Ops"} · {s.id.slice(-8)}</span>
              <span className="sess-time">{formatAgo(s.time)}</span>
            </div>
          ))}
        </div>
      )}

      <div style={{ marginTop: "auto" }}>
        <div className="sb-section">
          <div className="sb-label">AWS Services</div>
          <div className="aws-grid">
            {AWS_SERVICES.map(s => (
              <div key={s.id} className="aws-chip">
                <div className={`dot ${healthDot(s.id.toLowerCase()) || "dot-grey"}`} />
                {s.label}
              </div>
            ))}
          </div>
          <div style={{ padding: "2px 10px 4px" }}>
            <div className="aws-chip" style={{ margin: "0" }}>
              <div className={`dot ${health.bedrock === "connected" ? "dot-green" : "dot-grey"}`} />
              Bedrock
            </div>
          </div>
        </div>
      </div>
    </aside>
  );

  // ── Status bar ────────────────────────────────────────────────────────
  const StatusBar = () => (
    <div className="status-bar">
      <div className="stat-chip">
        <div className={`dot ${health.nova_sonic === "connected" ? "dot-green" : "dot-grey"}`} />
        Sonic {latency ? `${latency}ms` : "—"}
      </div>
      <div className="agent-pills">
        {["Orchestrator", "Security", "LogParser", "RCA"].map(a => (
          <div key={a} className={`agent-pill ${activeAgents.includes(a) ? "pill-active" : "pill-idle"}`}>{a}</div>
        ))}
      </div>
      <div className="stat-chip" style={{ marginLeft: "auto" }}>
        <span style={{ color: "var(--cyan)" }}>◈</span>
        {indexChunks ? `${indexChunks.toLocaleString()} chunks` : "—"}
      </div>
    </div>
  );

  // ── Right panel ────────────────────────────────────────────────────────
  const RightPanel = () => {
    const issueCount = actions.filter(a => a.type === "github_issue" && a.status === "success").length;
    const prCount = actions.filter(a => a.type === "github_pr" && a.status === "success").length;
    return (
      <aside className="right">
        <div className="right-header">
          <span className="right-title">⚡ Live Actions</span>
          <span style={{ fontSize: 10, color: "var(--text2)", fontFamily: "var(--mono)" }}>{actions.length} total</span>
        </div>
        <div className="right-body">
          {actions.length === 0 && (
            <div className="empty-state">
              <span style={{ fontSize: 22 }}>◎</span>
              <span>No actions yet</span>
            </div>
          )}
          {actions.map(a => (
            <div key={a.action_id} className="action-item">
              <div className="action-row">
                <span className="action-icon">{actionIcon(a.type, a.status)}</span>
                <span className="action-desc">{a.description}</span>
              </div>
              <div className="action-meta">
                <span className={`action-status status-${a.status}`}>{a.status.replace("_", " ")}</span>
                <span className="action-time">{formatAgo(a.timestamp)}</span>
              </div>
              {a.url && <a href={a.url} target="_blank" rel="noreferrer" style={{ fontSize: 10, color: "var(--cyan)", fontFamily: "var(--mono)", display: "block", marginTop: 4, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{a.url}</a>}
            </div>
          ))}
        </div>
        <div className="summary-cards">
          <div className="summary-card">
            <div className="sc-num">{issueCount}</div>
            <div className="sc-label">Issues Filed</div>
          </div>
          <div className="summary-card">
            <div className="sc-num">{prCount}</div>
            <div className="sc-label">PRs Opened</div>
          </div>
        </div>
      </aside>
    );
  };

  // ── Screens ────────────────────────────────────────────────────────────
  const LandingScreen = () => (
    <div className="landing">
      <div className="landing-logo">V</div>
      <div>
        <div className="landing-title">Your AI Staff Engineer</div>
        <div style={{ fontSize: 11, color: "var(--cyan)", fontFamily: "var(--mono)", textAlign: "center", marginTop: 4, letterSpacing: 0.5 }}>every developer deserves a staff engineer at 3am</div>
      </div>
      <div className="landing-sub">Index a GitHub repository to get started. Vega will analyze your codebase and be ready to answer security, architecture, and incident questions by voice.</div>
      <div className="landing-form">
        <div className="form-label">GitHub Repository URL</div>
        <input className="form-input" placeholder="https://github.com/org/repo" value={repoUrl} onChange={e => setRepoUrl(e.target.value)} onKeyDown={e => e.key === "Enter" && indexRepo()} />
        <div style={{ display: "flex", gap: 8 }}>
          <div style={{ flex: 1 }}>
            <div className="form-label" style={{ marginBottom: 4 }}>Branch</div>
            <input className="form-input" placeholder="main" value={branch} onChange={e => setBranch(e.target.value)} />
          </div>
          <div style={{ flex: 2 }}>
            <div className="form-label" style={{ marginBottom: 4 }}>GitHub Token (optional)</div>
            <input className="form-input" placeholder="ghp_..." type="password" value={githubToken} onChange={e => setGithubToken(e.target.value)} />
          </div>
        </div>
        <button className="submit-btn" onClick={indexRepo} disabled={!repoUrl.trim() || isIndexing}>
          {isIndexing ? "Indexing…" : "⬡  Index Repository"}
        </button>
      </div>
    </div>
  );

  const IndexingScreen = () => {
    const pct = Math.round(indexProgress);
    return (
      <div className="indexing-panel">
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div className="spinner" />
          <span style={{ fontFamily: "var(--mono)", fontSize: 13, color: "var(--cyan)" }}>Indexing repository…</span>
        </div>
        <div className="progress-box">
          <div className="progress-title">{repoShort || indexedRepo?.url}</div>
          <div className="progress-sub" style={{ fontFamily: "var(--mono)" }}>Branch: {indexedRepo?.branch} · Generating embeddings via Nova Multimodal</div>
          <div className="progress-bar-bg">
            <div className="progress-bar-fill" style={{ width: `${pct}%` }} />
          </div>
          <div className="progress-stats">
            <span>{pct}% complete</span>
            <span>{indexChunks.toLocaleString()} / {indexTotal.toLocaleString()} chunks</span>
          </div>
        </div>
        <div style={{ fontSize: 11, color: "var(--text2)", fontFamily: "var(--mono)", display: "flex", gap: 16 }}>
          <span>FAISS vectorstore</span>
          <span style={{ color: health.bedrock === "connected" ? "var(--green)" : "var(--text2)" }}>▸ Bedrock {health.bedrock || "—"}</span>
          <span style={{ color: "var(--cyan)" }}>◈ Nova Multimodal Embeddings</span>
        </div>
      </div>
    );
  };

  const ReadyScreen = () => (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <div style={{ padding: "14px 20px 0", borderBottom: "1px solid var(--border)", flexShrink: 0, background: "var(--bg1)" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
          <div>
            <div style={{ fontWeight: 600, fontSize: 14 }}>◈ {repoShort}</div>
            <div style={{ fontSize: 11, color: "var(--text2)", fontFamily: "var(--mono)", marginTop: 2 }}>{indexFileCount} files · {indexChunks.toLocaleString()} chunks indexed</div>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={loadIntelligence} style={{ background: "rgba(191,90,242,0.12)", border: "1px solid rgba(191,90,242,0.3)", color: "var(--purple)", padding: "6px 12px", borderRadius: 7, fontSize: 12, cursor: "pointer", fontFamily: "var(--sans)", fontWeight: 500 }}>
              ◈ Intelligence
            </button>
            <button onClick={() => startSession("dev")} style={{ background: "rgba(10,132,255,0.12)", border: "1px solid rgba(10,132,255,0.3)", color: "var(--blue)", padding: "6px 14px", borderRadius: 7, fontSize: 12, cursor: "pointer", fontFamily: "var(--sans)", fontWeight: 500 }}>
              ⬡ Dev Mode
            </button>
            <button onClick={() => startSession("ops")} style={{ background: "rgba(255,69,58,0.12)", border: "1px solid rgba(255,69,58,0.3)", color: "var(--red)", padding: "6px 14px", borderRadius: 7, fontSize: 12, cursor: "pointer", fontFamily: "var(--sans)", fontWeight: 500 }}>
              ◈ Ops Mode
            </button>
          </div>
        </div>
        <div className="tab-bar" style={{ border: "none" }}>
          {["diagram", "intelligence"].map(t => (
            <div key={t} className={`tab ${activeTab === t ? "active" : ""}`} onClick={() => setActiveTab(t)}>
              {t.charAt(0).toUpperCase() + t.slice(1)}
            </div>
          ))}
        </div>
      </div>
      <div style={{ flex: 1, overflow: "auto", padding: 20 }}>
        {activeTab === "diagram" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 10, height: "100%" }}>
            {/* View mode toggle */}
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 6, flexShrink: 0 }}>
              <button
                onClick={() => setViewMode("architecture")}
                style={{
                  background: viewMode === "architecture" ? "rgba(0,229,200,0.12)" : "transparent",
                  border: `1px solid ${viewMode === "architecture" ? "rgba(0,229,200,0.4)" : "var(--border)"}`,
                  color: viewMode === "architecture" ? "var(--cyan)" : "var(--text2)",
                  padding: "4px 12px", borderRadius: 6, fontSize: 11, cursor: "pointer", fontFamily: "var(--mono)",
                }}
              >
                Architecture
              </button>
              <button
                onClick={() => setViewMode("build-status")}
                style={{
                  background: viewMode === "build-status" ? "rgba(191,90,242,0.12)" : "transparent",
                  border: `1px solid ${viewMode === "build-status" ? "rgba(191,90,242,0.4)" : "var(--border)"}`,
                  color: viewMode === "build-status" ? "var(--purple)" : "var(--text2)",
                  padding: "4px 12px", borderRadius: 6, fontSize: 11, cursor: "pointer", fontFamily: "var(--mono)",
                }}
              >
                Build Status
              </button>
            </div>
            {/* Diagram canvas */}
            <div className="diagram-wrap" style={{ flex: 1, padding: 0, alignItems: "stretch", minHeight: 300 }}>
              <VegaDiagram
                nodes={diagramData.nodes}
                edges={diagramData.edges}
                highlightedNodes={highlightedNodes}
                viewMode={viewMode}
                buildStatus={buildStatusData}
                onNodeClick={(id) => console.log("[Vega] node clicked:", id)}
              />
            </div>
          </div>
        )}
        {activeTab === "intelligence" && (
          intelCards.length > 0 ? (
            <div className="intel-grid">
              {intelCards.map((c, i) => (
                <div key={i} className="intel-card">
                  <div className="intel-badge">{c.priority || "SUGGESTION"}</div>
                  <div className="intel-title">{c.title || c.suggestion?.slice(0, 50)}</div>
                  <div className="intel-body">{c.body || c.rationale || c.suggestion}</div>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", flexDirection: "column", gap: 12 }}>
              <div style={{ fontSize: 32 }}>◈</div>
              <div style={{ color: "var(--text2)", fontFamily: "var(--mono)", fontSize: 12 }}>Click "Intelligence" to run gap analysis</div>
            </div>
          )
        )}
      </div>
    </div>
  );

  const SessionScreen = () => {
    const mc = MODE_CONFIG[mode];
    return (
      <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", position: "relative" }}>
        {/* Dev→Ops transition flash overlay */}
        <div
          className={isTransitioning ? "mode-transition-flash" : ""}
          style={{
            position: "absolute", inset: 0, pointerEvents: "none", zIndex: 5,
            display: isTransitioning ? "block" : "none",
          }}
        />

        {/* ModeIndicator — absolute top-right, read-only reactive badge */}
        {sessionId && (
          <div style={{ position: "absolute", top: 16, right: 16, zIndex: 10 }}>
            <ModeIndicator
              mode={currentMode}
              modeFamily={modeFamily}
              isTransitioning={isTransitioning}
            />
          </div>
        )}

        {/* mode banner */}
        <div className="mode-banner">
          <div className="mode-badge">
            <span className="mode-icon">{mc.icon}</span>
            <div>
              <div className="mode-name" style={{ color: mc.color }}>{mc.label}</div>
              <div className="mode-sub">{mc.desc}</div>
            </div>
          </div>
          <div className="mode-toggle">
            <button onClick={() => { setScreen("ready"); wsRef.current?.close(); stopMic(); }} style={{ padding: "5px 10px", borderRadius: 6, border: "1px solid var(--border)", background: "transparent", color: "var(--text2)", fontSize: 11, cursor: "pointer", fontFamily: "var(--sans)" }}>← Repo</button>
          </div>
        </div>

        {/* transcript */}
        <div className="transcript-area">
          {messages.length === 0 && !partialTranscript && (
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", flex: 1, gap: 10, opacity: 0.4 }}>
              <div style={{ fontSize: 36 }}>{mc.icon}</div>
              <div style={{ fontFamily: "var(--mono)", fontSize: 12, color: "var(--text2)", textAlign: "center" }}>
                {mode === "dev" ? "Say: 'Review my auth module for security issues'" : "Say: 'My Lambda function is failing in prod, find out why'"}
              </div>
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role === "user" ? "user" : ""}`}>
              <div className={`msg-avatar ${m.role === "vega" ? "avatar-vega" : "avatar-user"}`}>
                {m.role === "vega" ? "V" : "U"}
              </div>
              <div className="msg-bubble">
                <div className={`bubble-text ${m.isError ? "status-failed" : ""}`}>{m.content}</div>
                <div className="bubble-time">{formatTime(m.ts)}</div>
              </div>
            </div>
          ))}
          {partialTranscript && (
            <div className="msg user">
              <div className="msg-avatar avatar-user">U</div>
              <div className="msg-bubble">
                <div className="bubble-text partial-text">{partialTranscript}<span style={{ opacity: 0.5 }}>▌</span></div>
              </div>
            </div>
          )}
          {micState === "processing" && (
            <div className="vega-thinking">
              <div className="msg-avatar avatar-vega">V</div>
              <div className="thinking-dots"><span /><span /><span /></div>
            </div>
          )}
          <div ref={transcriptEndRef} />
        </div>

        {/* highlighted nodes display */}
        {highlightedNodes.length > 0 && (
          <div style={{ padding: "6px 20px", background: "rgba(0,229,200,0.05)", borderTop: "1px solid rgba(0,229,200,0.15)", display: "flex", gap: 6, flexWrap: "wrap" }}>
            <span style={{ fontSize: 10, color: "var(--text2)", fontFamily: "var(--mono)" }}>HIGHLIGHTING</span>
            {highlightedNodes.map(n => (
              <span key={n} style={{ background: "rgba(0,229,200,0.15)", border: "1px solid rgba(0,229,200,0.3)", color: "var(--cyan)", borderRadius: 4, padding: "1px 7px", fontSize: 10, fontFamily: "var(--mono)" }}>{n}</span>
            ))}
          </div>
        )}

        {/* mic area */}
        <div className="mic-area">
          <button
            className={`mic-btn ${micState}`}
            onClick={toggleRecording}
            title={micState === "idle" ? "Click to speak" : micState === "listening" ? "Click to stop" : "Processing…"}
          >
            {micState === "idle" ? "🎙" : micState === "listening" ? "⏹" : <div className="spinner" style={{ width: 20, height: 20 }} />}
          </button>
          <div style={{ display: "flex", flexDirection: "column", gap: 4, flex: 1 }}>
            <div className="waveform">
              {waveData.map((h, i) => (
                <div key={i} className="wave-bar" style={{ height: h, opacity: micState === "listening" ? 0.8 : 0.2 }} />
              ))}
            </div>
            <div className="mic-label">
              {micState === "idle" && "Click mic or press Space to speak"}
              {micState === "listening" && <span style={{ color: "var(--cyan)" }}>● Listening… (click to stop)</span>}
              {micState === "processing" && <span style={{ color: "var(--blue)" }}>Processing with Nova…</span>}
            </div>
          </div>
        </div>
      </div>
    );
  };

  // ── Index modal ────────────────────────────────────────────────────────
  const IndexModal = () => (
    <div className="index-modal" onClick={() => setShowIndexModal(false)}>
      <div className="index-modal-box" onClick={e => e.stopPropagation()}>
        <div style={{ fontWeight: 600, fontSize: 15, marginBottom: 14 }}>Index New Repository</div>
        <div className="form-label" style={{ marginBottom: 4 }}>Repository URL</div>
        <input className="form-input" style={{ width: "100%", marginBottom: 10 }} placeholder="https://github.com/org/repo" value={repoUrl} onChange={e => setRepoUrl(e.target.value)} />
        <div style={{ display: "flex", gap: 8, marginBottom: 14 }}>
          <div style={{ flex: 1 }}>
            <div className="form-label" style={{ marginBottom: 4 }}>Branch</div>
            <input className="form-input" style={{ width: "100%" }} value={branch} onChange={e => setBranch(e.target.value)} placeholder="main" />
          </div>
        </div>
        <div className="form-label" style={{ marginBottom: 4 }}>GitHub Token (private repos)</div>
        <input className="form-input" style={{ width: "100%", marginBottom: 16 }} type="password" placeholder="ghp_…" value={githubToken} onChange={e => setGithubToken(e.target.value)} />
        <div style={{ display: "flex", gap: 8 }}>
          <button className="submit-btn" style={{ flex: 1 }} onClick={indexRepo} disabled={!repoUrl.trim() || isIndexing}>{isIndexing ? "Indexing…" : "Index"}</button>
          <button onClick={() => setShowIndexModal(false)} style={{ flex: 1, background: "var(--bg3)", border: "1px solid var(--border)", color: "var(--text1)", borderRadius: 8, padding: "11px", cursor: "pointer", fontFamily: "var(--sans)" }}>Cancel</button>
        </div>
      </div>
    </div>
  );

  // ── Confirmation modal ─────────────────────────────────────────────────
  const ConfirmModal = () => confirmation && (
    <div className="modal-overlay">
      <div className="modal">
        <div className="modal-icon">🔒</div>
        <div className="modal-title">Action Confirmation Required</div>
        <div className="modal-body">{confirmation.prompt}</div>
        <div className="modal-btns">
          <button className="modal-confirm" onClick={() => handleConfirm(true)}>✓ Confirm</button>
          <button className="modal-reject" onClick={() => handleConfirm(false)}>✕ Reject</button>
        </div>
      </div>
    </div>
  );

  // ── Space to toggle mic ─────────────────────────────────────────────────
  useEffect(() => {
    if (screen !== "session") return;
    const handler = (e) => { if (e.code === "Space" && e.target === document.body) { e.preventDefault(); toggleRecording(); } };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [screen, isRecording, micState]);

  // ── Render ─────────────────────────────────────────────────────────────
  return (
    <>
      <style dangerouslySetInnerHTML={{ __html: css }} />
      <div className="shell">
        <Sidebar />
        <div className="center">
          {screen === "landing" && <LandingScreen />}
          {screen === "indexing" && <IndexingScreen />}
          {screen === "ready" && <ReadyScreen />}
          {screen === "session" && <SessionScreen />}
          <StatusBar />
        </div>
        {(screen === "session" || screen === "ready") && <RightPanel />}
      </div>
      {showIndexModal && <IndexModal />}
      {confirmation && <ConfirmModal />}
    </>
  );
}
