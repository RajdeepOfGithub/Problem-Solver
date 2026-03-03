"use client";
import { useState, useEffect, useMemo } from "react";

// ── Layout constants ──────────────────────────────────────────────────────────
const NODE_W = 160;
const NODE_H = 64;
const H_GAP = 200; // gap between right edge of one layer and left edge of next
const V_GAP = 80;  // gap between bottom of one node and top of next
const PAD = 60;

// ── Build-status overlay styles ───────────────────────────────────────────────
const BUILD_STATUS_STYLES = {
  complete: { fill: "#052e16", border: "#16a34a", label: "COMPLETE", labelColor: "#4ade80" },
  planned:  { fill: "#451a03", border: "#d97706", label: "PLANNED",  labelColor: "#fbbf24" },
  research: { fill: "#1e1b4b", border: "#6366f1", label: "RESEARCH", labelColor: "#a5b4fc" },
};

const FILE_TYPE_BADGE = {
  notebook: "NOTEBOOK",
  model:    "MODEL",
  data:     "DATA",
  config:   "CONFIG",
  python:   "PYTHON",
  folder:   "FOLDER",
  other:    "FILE",
};

// ── Topological layer layout ──────────────────────────────────────────────────
function computeLayout(nodes, edges) {
  if (!nodes || nodes.length === 0) {
    return { positions: {}, svgWidth: 400, svgHeight: 200 };
  }

  const nodeIds = new Set(nodes.map(n => n.id));
  const inDegree = {};
  const adj = {};
  nodes.forEach(n => { inDegree[n.id] = 0; adj[n.id] = []; });
  edges.forEach(e => {
    if (nodeIds.has(e.source) && nodeIds.has(e.target)) {
      inDegree[e.target] = (inDegree[e.target] || 0) + 1;
      if (adj[e.source]) adj[e.source].push(e.target);
    }
  });

  // BFS layering — layer 0 = nodes with no incoming edges
  const layers = [];
  let queue = nodes.filter(n => inDegree[n.id] === 0).map(n => n.id);
  if (queue.length === 0) queue = [nodes[0].id]; // fallback
  const visited = new Set(queue);

  while (queue.length > 0) {
    layers.push([...queue]);
    const next = [];
    queue.forEach(id => {
      (adj[id] || []).forEach(child => {
        if (!visited.has(child)) {
          visited.add(child);
          next.push(child);
        }
      });
    });
    queue = next;
  }

  // Any disconnected nodes go in a final layer
  const unvisited = nodes.filter(n => !visited.has(n.id)).map(n => n.id);
  if (unvisited.length > 0) layers.push(unvisited);

  const maxLayerSize = Math.max(...layers.map(l => l.length));
  const svgWidth  = layers.length * (NODE_W + H_GAP) - H_GAP + 2 * PAD;
  const svgHeight = maxLayerSize * (NODE_H + V_GAP)  - V_GAP  + 2 * PAD;

  // Assign positions (center-based) — each layer is vertically centered
  const positions = {};
  layers.forEach((layer, li) => {
    const cx = PAD + NODE_W / 2 + li * (NODE_W + H_GAP);
    const layerH = layer.length * (NODE_H + V_GAP) - V_GAP;
    const startY = (svgHeight - layerH) / 2 + NODE_H / 2;
    layer.forEach((id, ni) => {
      positions[id] = { x: cx, y: startY + ni * (NODE_H + V_GAP) };
    });
  });

  return { positions, svgWidth, svgHeight };
}

// ── Component ─────────────────────────────────────────────────────────────────
export default function VegaDiagram({
  nodes = [],
  edges = [],
  highlightedNodes = [],
  viewMode = "architecture",
  buildStatus = {},
  onNodeClick,
}) {
  const [selectedNode, setSelectedNode] = useState(null);
  const [blastSet, setBlastSet]         = useState(new Set());
  const [hoveredNode, setHoveredNode]   = useState(null);
  const [mounted, setMounted]           = useState(false);

  // Staggered mount trigger
  useEffect(() => {
    const t = setTimeout(() => setMounted(true), 30);
    return () => clearTimeout(t);
  }, []);

  // Reset selection when diagram data changes
  useEffect(() => {
    setSelectedNode(null);
    setBlastSet(new Set());
  }, [nodes]);

  const { positions, svgWidth, svgHeight } = useMemo(
    () => computeLayout(nodes, edges),
    [nodes, edges]
  );

  // Build downstream adjacency for blast-radius calculation
  const downAdj = useMemo(() => {
    const adj = {};
    nodes.forEach(n => { adj[n.id] = []; });
    edges.forEach(e => {
      if (adj[e.source]) adj[e.source].push(e.target);
    });
    return adj;
  }, [nodes, edges]);

  function getDownstream(startId) {
    const result = new Set();
    const q = [...(downAdj[startId] || [])];
    while (q.length > 0) {
      const id = q.shift();
      if (!result.has(id)) {
        result.add(id);
        (downAdj[id] || []).forEach(c => q.push(c));
      }
    }
    return result;
  }

  function handleNodeClick(nodeId) {
    if (selectedNode === nodeId) {
      setSelectedNode(null);
      setBlastSet(new Set());
      onNodeClick?.(null);
    } else {
      setSelectedNode(nodeId);
      setBlastSet(getDownstream(nodeId));
      onNodeClick?.(nodeId);
    }
  }

  // ── Fallback ────────────────────────────────────────────────────────────────
  if (!nodes || nodes.length === 0) {
    return (
      <div style={{
        width: "100%", height: "100%", minHeight: 200, background: "#000",
        display: "flex", flexDirection: "column", alignItems: "center",
        justifyContent: "center", gap: 12, padding: 24, fontFamily: "monospace",
      }}>
        <div style={{ fontSize: 32, opacity: 0.2 }}>◈</div>
        <div style={{ fontSize: 11, color: "rgba(255,255,255,0.3)", letterSpacing: 1 }}>
          NO DIAGRAM DATA
        </div>
        <div style={{ fontSize: 12, color: "rgba(255,255,255,0.2)", textAlign: "center", maxWidth: 280 }}>
          Index a repository to generate the architecture diagram.
        </div>
      </div>
    );
  }

  // Delay at which edges appear (after all nodes have started mounting)
  const edgeDelay = nodes.length * 50 + 100;

  return (
    <div style={{ width: "100%", height: "100%", minHeight: 200, background: "#000", overflow: "auto" }}>
      {/* CSS keyframes — scoped via unique class prefix */}
      <style>{`
        @keyframes vegaPulse {
          0%, 100% { filter: drop-shadow(0 0 0px #06b6d4); }
          50%       { filter: drop-shadow(0 0 8px #06b6d4); }
        }
        @keyframes vegaNodeIn {
          from { opacity: 0; transform: translateY(6px); }
          to   { opacity: 1; transform: translateY(0); }
        }
        @keyframes vegaEdgeIn {
          from { opacity: 0; }
          to   { opacity: 1; }
        }
      `}</style>

      <svg
        width={svgWidth}
        height={svgHeight}
        viewBox={`0 0 ${svgWidth} ${svgHeight}`}
        style={{ display: "block" }}
      >
        <defs>
          {/* Dot-grid texture */}
          <pattern id="vd-dots" x="0" y="0" width="20" height="20" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="0.8" fill="rgba(255,255,255,0.06)" />
          </pattern>
        </defs>

        {/* Pure black background + dot grid */}
        <rect width={svgWidth} height={svgHeight} fill="#000000" />
        <rect width={svgWidth} height={svgHeight} fill="url(#vd-dots)" />

        {/* ── Edges (rendered before nodes so they sit beneath) ── */}
        {edges.map((edge, ei) => {
          const src = positions[edge.source];
          const tgt = positions[edge.target];
          if (!src || !tgt) return null;

          // Exit right edge of source, enter left edge of target
          const sx = src.x + NODE_W / 2;
          const sy = src.y;
          const tx = tgt.x - NODE_W / 2;
          const ty = tgt.y;

          // Cubic bezier control points — offset horizontally
          const cp = Math.max(60, Math.abs(tx - sx) * 0.4);
          const d = `M ${sx} ${sy} C ${sx + cp} ${sy}, ${tx - cp} ${ty}, ${tx} ${ty}`;

          return (
            <g
              key={edge.id || `e${ei}`}
              style={
                mounted
                  ? { animation: `vegaEdgeIn 300ms ease ${edgeDelay}ms both` }
                  : { opacity: 0 }
              }
            >
              <path
                d={d}
                fill="none"
                stroke="rgba(255,255,255,0.2)"
                strokeWidth="1.5"
                strokeLinecap="round"
              />
              {/* Junction dots */}
              <circle cx={sx} cy={sy} r="3.5" fill="rgba(255,255,255,0.45)" />
              <circle cx={tx} cy={ty} r="3.5" fill="rgba(255,255,255,0.45)" />
            </g>
          );
        })}

        {/* ── Nodes ── */}
        {nodes.map((node, ni) => {
          const pos = positions[node.id];
          if (!pos) return null;

          const nx = pos.x - NODE_W / 2;
          const ny = pos.y - NODE_H / 2;

          const isVoiceHighlighted = highlightedNodes.includes(node.id);
          const isSelected         = selectedNode === node.id;
          const isBlast            = blastSet.has(node.id);
          const isHovered          = hoveredNode === node.id;

          // Build-status style overrides
          const bsStyle = viewMode === "build-status" && buildStatus[node.id]
            ? (BUILD_STATUS_STYLES[buildStatus[node.id]] || null)
            : null;

          // Fill color
          let fill = bsStyle ? bsStyle.fill : "#0f1117";
          if (isSelected) fill = "rgba(6,182,212,0.10)";
          if (isBlast)    fill = "rgba(251,191,36,0.07)";

          // Border stroke
          let borderStroke = bsStyle
            ? bsStyle.border
            : isSelected ? "#06b6d4"
            : isBlast    ? "#fbbf24"
            : isHovered  ? "rgba(255,255,255,0.25)"
            : "rgba(255,255,255,0.08)";

          const accentColor = bsStyle
            ? bsStyle.border
            : (node.accent_color || "#374151");

          const badge    = FILE_TYPE_BADGE[node.file_type] || "FILE";
          const labelTxt = node.label.length > 17
            ? node.label.slice(0, 15) + "…"
            : node.label;

          // Animation: pulse takes priority over mount (mount is already complete by then)
          let animation;
          if (!mounted) {
            animation = undefined;
          } else if (isVoiceHighlighted) {
            animation = "vegaPulse 0.8s ease infinite";
          } else {
            animation = `vegaNodeIn 300ms ease ${ni * 50}ms both`;
          }

          return (
            <g
              key={node.id}
              transform={`translate(${nx}, ${ny})`}
              onClick={() => handleNodeClick(node.id)}
              onMouseEnter={() => setHoveredNode(node.id)}
              onMouseLeave={() => setHoveredNode(null)}
              style={{
                cursor: "pointer",
                opacity: mounted ? undefined : 0,
                animation,
              }}
            >
              {/* Card background */}
              <rect
                width={NODE_W}
                height={NODE_H}
                rx="6"
                fill={fill}
                stroke={borderStroke}
                strokeWidth="1"
              />

              {/* Accent bar — 3px at bottom */}
              <rect
                x="0"
                y={NODE_H - 3}
                width={NODE_W}
                height="3"
                rx="1.5"
                fill={accentColor}
              />

              {/* File type badge (top-left, muted) */}
              <text
                x="10"
                y="17"
                fill="rgba(255,255,255,0.30)"
                fontSize="8"
                fontFamily="monospace"
                fontWeight="600"
                letterSpacing="0.8"
              >
                {badge}
              </text>

              {/* Node label */}
              <text
                x="10"
                y="38"
                fill="white"
                fontSize="12"
                fontFamily="monospace"
              >
                {labelTxt}
              </text>

              {/* Build-status overlay badge (top-right) */}
              {bsStyle && (
                <text
                  x={NODE_W - 8}
                  y="17"
                  fill={bsStyle.labelColor}
                  fontSize="7"
                  fontFamily="monospace"
                  fontWeight="700"
                  textAnchor="end"
                >
                  {bsStyle.label}
                </text>
              )}

              {/* Extra selection ring for selected node */}
              {isSelected && (
                <rect
                  width={NODE_W}
                  height={NODE_H}
                  rx="6"
                  fill="none"
                  stroke="#06b6d4"
                  strokeWidth="1.5"
                />
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
