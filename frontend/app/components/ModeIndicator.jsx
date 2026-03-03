"use client";

// ── Sub-mode label map ────────────────────────────────────────────────────────
const MODE_LABELS = {
  dev_explore:     "EXPLORE · DEV",
  dev_review:      "REVIEW · DEV",
  dev_build:       "BUILD · DEV→OPS",
  ops_incident:    "INCIDENT · OPS",
  ops_code_action: "ACTION · OPS",
  ambiguous:       "LISTENING...",
};

export default function ModeIndicator({
  mode           = "dev_explore",
  modeFamily     = "dev",
  isTransitioning = false,
}) {
  const label      = MODE_LABELS[mode] || mode.toUpperCase();
  const isAmbiguous = mode === "ambiguous";
  const isOps       = modeFamily === "ops";

  // Colors
  const devBorder  = "rgba(99,102,241,0.5)";
  const opsBorder  = "rgba(220,38,38,0.5)";
  const devDot     = "#6366f1";
  const opsDot     = "#dc2626";
  const ambigBorder = "rgba(107,114,128,0.4)";
  const ambigDot   = "#6b7280";

  const dotColor    = isAmbiguous ? ambigDot   : isOps ? opsDot   : devDot;
  const borderColor = isAmbiguous ? ambigBorder : isTransitioning ? devBorder : isOps ? opsBorder : devBorder;
  const textColor   = isAmbiguous ? "rgba(255,255,255,0.35)" : "rgba(255,255,255,0.80)";

  return (
    <>
      <style>{`
        @keyframes miDotPulse {
          0%, 100% { transform: scale(0.8); opacity: 0.65; }
          50%       { transform: scale(1.0); opacity: 1.0;  }
        }
        @keyframes miBorderTransition {
          0%   { border-color: rgba(99,102,241,0.5); }
          60%  { border-color: rgba(220,38,38,0.5);  }
          100% { border-color: rgba(220,38,38,0.5);  }
        }
      `}</style>

      <div
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 7,
          padding: "6px 14px",
          borderRadius: 999,
          border: `1px solid ${borderColor}`,
          background: "transparent",
          animation: isTransitioning ? "miBorderTransition 600ms ease forwards" : undefined,
          pointerEvents: "none",
          userSelect: "none",
          flexShrink: 0,
        }}
      >
        {/* Live indicator dot — always softly pulsing */}
        <span
          style={{
            display: "inline-block",
            width: 7,
            height: 7,
            borderRadius: "50%",
            background: dotColor,
            animation: "miDotPulse 2s ease infinite",
            flexShrink: 0,
          }}
        />

        {/* Mode label */}
        <span
          style={{
            fontFamily: "monospace",
            fontSize: 11,
            letterSpacing: "0.1em",
            color: textColor,
            whiteSpace: "nowrap",
          }}
        >
          {label}
        </span>
      </div>
    </>
  );
}
