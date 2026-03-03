"use client";

export default function ModeIndicator({ mode = "dev_explore", modeFamily = "dev", isTransitioning = false }) {
  const labels = {
    dev_explore:     "EXPLORE · DEV",
    dev_review:      "REVIEW · DEV",
    dev_build:       "BUILD · DEV→OPS",
    ops_incident:    "INCIDENT · OPS",
    ops_code_action: "ACTION · OPS",
    ambiguous:       "LISTENING...",
  };

  const devStyle   = { border: "1px solid rgba(99,102,241,0.5)",  dotColor: "#6366f1" };
  const opsStyle   = { border: "1px solid rgba(220,38,38,0.5)",   dotColor: "#dc2626" };
  const ambigStyle = { border: "1px solid rgba(255,255,255,0.15)", dotColor: "#64748b" };

  const s = mode === "ambiguous" ? ambigStyle : modeFamily === "ops" ? opsStyle : devStyle;

  return (
    <>
      <style>{`
        @keyframes dotPulse {
          0%, 100% { transform: scale(1); opacity: 1; }
          50% { transform: scale(0.75); opacity: 0.6; }
        }
        @keyframes modeBorderPulse {
          0%   { border-color: rgba(99,102,241,0.5); }
          50%  { border-color: rgba(220,38,38,0.8); }
          100% { border-color: rgba(220,38,38,0.5); }
        }
      `}</style>
      <div
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: "8px",
          padding: "6px 14px",
          borderRadius: "999px",
          border: s.border,
          fontFamily: "monospace",
          fontSize: "11px",
          letterSpacing: "0.1em",
          color: "rgba(255,255,255,0.8)",
          userSelect: "none",
          pointerEvents: "none",
          animation: isTransitioning ? "modeBorderPulse 0.6s ease" : "none",
        }}
      >
        <span
          style={{
            width: "7px",
            height: "7px",
            borderRadius: "50%",
            backgroundColor: s.dotColor,
            animation: "dotPulse 2s ease infinite",
            flexShrink: 0,
            display: "inline-block",
          }}
        />
        {labels[mode] || "EXPLORE · DEV"}
      </div>
    </>
  );
}
