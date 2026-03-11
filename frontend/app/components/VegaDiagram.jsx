"use client";
import { useState, useEffect, useMemo } from "react";

// ── Layout constants ──────────────────────────────────────────────────────────
const NODE_W = 140;
const NODE_H = 48;
const H_GAP = 200; // horizontal space between layers
const V_GAP = 12;  // vertical gap between nodes — tight
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
  if (!nodes || nodes.length === 0) return {}

  // Build adjacency: outgoing edges and incoming count per node
  const outgoing = {}
  const incomingCount = {}
  nodes.forEach(n => {
    outgoing[n.id] = []
    incomingCount[n.id] = 0
  })
  edges.forEach(e => {
    if (outgoing[e.source] !== undefined) outgoing[e.source].push(e.target)
    if (incomingCount[e.target] !== undefined) incomingCount[e.target]++
  })

  // Kahn's algorithm — assign each node a layer number
  const layer = {}
  const queue = nodes.filter(n => incomingCount[n.id] === 0).map(n => n.id)

  if (queue.length === 0) {
    // All nodes in a cycle — put everything in layer 0
    nodes.forEach(n => { layer[n.id] = 0 })
  } else {
    queue.forEach(id => { layer[id] = 0 })
    const visited = new Set(queue)
    let head = 0
    while (head < queue.length) {
      const current = queue[head++]
      const currentLayer = layer[current]
      ;(outgoing[current] || []).forEach(targetId => {
        const newLayer = currentLayer + 1
        if (layer[targetId] === undefined || layer[targetId] < newLayer) {
          layer[targetId] = newLayer
        }
        if (!visited.has(targetId)) {
          visited.add(targetId)
          queue.push(targetId)
        }
      })
    }
    // Disconnected nodes fall into layer 0
    nodes.forEach(n => { if (layer[n.id] === undefined) layer[n.id] = 0 })
  }

  // Group nodes by layer
  const layerGroups = {}
  nodes.forEach(n => {
    const l = layer[n.id]
    if (!layerGroups[l]) layerGroups[l] = []
    layerGroups[l].push(n.id)
  })

  // Assign x/y positions — top-left corner, w/h for bounds
  const positions = {}
  const layerKeys = Object.keys(layerGroups).map(Number).sort((a, b) => a - b)

  if (layerKeys.length === 1) {
    // Grid layout for disconnected graphs (all nodes in one layer)
    const group = layerGroups[layerKeys[0]]
    const COLS = 3
    const COL_W = NODE_W + 16
    const ROW_H = NODE_H + 12

    group.forEach((id, i) => {
      const col = i % COLS
      const row = Math.floor(i / COLS)
      positions[id] = {
        x: PAD + col * COL_W,
        y: PAD + row * ROW_H,
        w: NODE_W,
        h: NODE_H,
      }
    })
  } else {
    // Layered layout for graphs with edges
    layerKeys.forEach((l, layerIndex) => {
      const group = layerGroups[l]
      const x = PAD + layerIndex * (NODE_W + H_GAP)
      const totalHeight = group.length * NODE_H + (group.length - 1) * V_GAP
      const startY = PAD + Math.max(0, (500 - totalHeight) / 2)
      group.forEach((id, i) => {
        positions[id] = {
          x,
          y: startY + i * (NODE_H + V_GAP),
          w: NODE_W,
          h: NODE_H,
        }
      })
    })
  }

  return positions
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

  const positions = useMemo(
    () => computeLayout(nodes, edges),
    [nodes, edges]
  );

  // Compute SVG canvas size from actual node positions
  const allPos = Object.values(positions);
  let svgWidth, svgHeight;
  if (allPos.length > 0) {
    svgWidth  = Math.max(...allPos.map(p => p.x + p.w)) + PAD;
    svgHeight = Math.max(...allPos.map(p => p.y + p.h)) + PAD;
  } else {
    svgWidth  = 800;
    svgHeight = 600;
  }

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
    <div style={{ width: "100%", height: "100%", overflow: "auto", background: "#000000", position: "relative" }}>
      {/* CSS keyframes — scoped via unique class prefix */}
      <style>{`
        @keyframes vegaPulse {
          0%, 100% { filter: drop-shadow(0 0 0px #06b6d4); }
          50%       { filter: drop-shadow(0 0 8px #06b6d4); }
        }
        @keyframes vegaNodeIn {
          from { opacity: 0; }
          to   { opacity: 1; }
        }
        @keyframes vegaEdgeIn {
          from { opacity: 0; }
          to   { opacity: 1; }
        }
      `}</style>

      <svg
        viewBox={`0 0 ${svgWidth} ${svgHeight}`}
        style={{ display: "block", minWidth: "100%", minHeight: "100%" }}
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

          // source node right-center → target node left-center
          const sx = src.x + src.w;
          const sy = src.y + src.h / 2;
          const tx = tgt.x;
          const ty = tgt.y + tgt.h / 2;
          const cx = (sx + tx) / 2;
          const d = `M ${sx} ${sy} C ${cx} ${sy}, ${cx} ${ty}, ${tx} ${ty}`;

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

          const nx = pos.x;
          const ny = pos.y;

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
          const labelTxt = node.label.length > 18
            ? node.label.slice(0, 16) + "…"
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
                rx="8"
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
                x="12"
                y="18"
                fill="rgba(255,255,255,0.30)"
                fontSize="9"
                fontFamily="monospace"
                fontWeight="600"
                letterSpacing="0.08em"
              >
                {badge}
              </text>

              {/* Node label */}
              <text
                x="12"
                y="38"
                fill="rgba(255,255,255,0.9)"
                fontSize="12"
                fontFamily="monospace"
                fontWeight="bold"
              >
                {labelTxt}
              </text>

              {/* Build-status overlay badge (top-right) */}
              {bsStyle && (
                <text
                  x={NODE_W - 10}
                  y="18"
                  fill={bsStyle.labelColor}
                  fontSize="8"
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
                  rx="8"
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
