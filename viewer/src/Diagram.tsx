import { useCallback, useEffect, useLayoutEffect, useRef, useState, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from "react";
import { FRAME_KINDS, roundedPath, type Placed, type PlacedNode } from "./layout";
import type { FlowNode } from "./types";

interface View {
  x: number;
  y: number;
  k: number;
}

interface Props {
  placed: Placed;
  /** Changes when a different function becomes the root: the view refits. */
  rootKey: string;
  /** Node that must stay put on screen after a relayout (the one just opened or closed). */
  anchor: string | null;
  fitSignal: number;
  selected: string | null;
  onSelect: (id: string | null) => void;
  onToggle: (node: FlowNode) => void;
  /** Width covered by the details panel on the right. */
  rightInset: number;
  /** Start following a piece of data from a node (absent: data labels are plain text). */
  onFollow?: (at: string, variable: string) => void;
  /** The diagram shows a data trail: steps cannot be opened or closed by hand. */
  following: boolean;
}

const clamp = (value: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, value));
const cx = (...names: (string | false | null | undefined)[]) => names.filter(Boolean).join(" ");

const IO_CHANNEL: Record<string, string> = { file: "file", db: "database", net: "network", env: "env", process: "process", console: "console" };
const IO_DIRECTION: Record<string, string> = { in: "in", out: "out", inout: "in/out" };

export function Diagram({ placed, rootKey, anchor, fitSignal, selected, onSelect, onToggle, rightInset, onFollow, following }: Props) {
  const viewport = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<View>({ x: 0, y: 0, k: 1 });
  const previous = useRef<{ rootKey: string; boxes: Map<string, PlacedNode> } | null>(null);

  // "Fit" shows everything; a new diagram instead opens at a readable zoom,
  // showing it whole when that fits, else from the top, centred on START.
  const fit = useCallback(() => {
    const el = viewport.current;
    if (!el) return;
    const { clientWidth: w, clientHeight: h } = el;
    const k = clamp(Math.min((w - 48) / placed.width, (h - 48) / placed.height), 0.1, 1);
    setView({ k, x: (w - placed.width * k) / 2, y: (h - placed.height * k) / 2 });
  }, [placed]);

  const openingView = useCallback(() => {
    const el = viewport.current;
    if (!el) return;
    const { clientWidth: w, clientHeight: h } = el;
    const whole = Math.min((w - 48) / placed.width, (h - 48) / placed.height, 1);
    if (whole >= 0.6) {
      setView({ k: whole, x: (w - placed.width * whole) / 2, y: (h - placed.height * whole) / 2 });
      return;
    }
    const k = clamp((w - 48) / placed.width, 0.6, 1);
    const start = placed.nodes.find((p) => p.node.kind === "start" && !p.node.parent);
    const centre = start ? start.x + start.width / 2 : placed.width / 2;
    setView({ k, x: w / 2 - centre * k, y: 24 });
  }, [placed]);

  // New root: opening view. Same root, new layout: keep the top-centre of the
  // step that was opened or closed where it was, zooming out only if it no
  // longer fits across.
  useLayoutEffect(() => {
    const before = previous.current;
    const boxes = new Map(placed.nodes.map((p) => [p.node.id, p]));
    if (!before || before.rootKey !== rootKey) {
      openingView();
    } else if (anchor) {
      const was = before.boxes.get(anchor);
      const now = boxes.get(anchor);
      const width = viewport.current?.clientWidth ?? 0;
      if (was && now) {
        setView((v) => {
          const sx = v.x + (was.x + was.width / 2) * v.k;
          const sy = v.y + was.y * v.k;
          const k = width && now.width * v.k > width - 48 ? clamp((width - 48) / now.width, 0.35, v.k) : v.k;
          return { k, x: sx - (now.x + now.width / 2) * k, y: sy - now.y * k };
        });
      }
    }
    previous.current = { rootKey, boxes };
  }, [placed, rootKey, anchor, openingView]);

  useEffect(() => {
    if (fitSignal) fit();
  }, [fitSignal, fit]);

  // Keep the selected node out from under the details panel.
  useEffect(() => {
    const el = viewport.current;
    const box = placed.nodes.find((p) => p.node.id === selected);
    if (!el || !box || !rightInset) return;
    setView((v) => {
      const right = v.x + (box.x + box.width) * v.k;
      const limit = el.clientWidth - rightInset - 24;
      return right > limit ? { ...v, x: v.x - (right - limit) } : v;
    });
  }, [selected, rightInset, placed]);

  // Wheel pans (long flows scroll like a document); ctrl/cmd + wheel or a pinch zooms.
  useEffect(() => {
    const el = viewport.current;
    if (!el) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      if (event.ctrlKey || event.metaKey) {
        const rect = el.getBoundingClientRect();
        const px = event.clientX - rect.left;
        const py = event.clientY - rect.top;
        setView((v) => {
          const k = clamp(v.k * Math.exp(-event.deltaY * 0.002), 0.1, 2.5);
          return { k, x: px - (px - v.x) * (k / v.k), y: py - (py - v.y) * (k / v.k) };
        });
      } else {
        setView((v) => ({ ...v, x: v.x - event.deltaX, y: v.y - event.deltaY }));
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  // Plain clicks select; the pointer is only captured once a drag really starts,
  // so clicks and double-clicks still reach the nodes.
  const drag = useRef<{ x: number; y: number; moved: boolean; pointer: number } | null>(null);
  const suppressClick = useRef(false);
  const onPointerDown = (event: ReactPointerEvent) => {
    if (event.button === 0) drag.current = { x: event.clientX, y: event.clientY, moved: false, pointer: event.pointerId };
  };
  const onPointerMove = (event: ReactPointerEvent) => {
    const d = drag.current;
    if (!d) return;
    const dx = event.clientX - d.x;
    const dy = event.clientY - d.y;
    if (!d.moved) {
      if (Math.hypot(dx, dy) < 4) return;
      d.moved = true;
      viewport.current?.setPointerCapture(d.pointer);
    }
    d.x = event.clientX;
    d.y = event.clientY;
    setView((v) => ({ ...v, x: v.x + dx, y: v.y + dy }));
  };
  const onPointerUp = () => {
    if (drag.current?.moved) suppressClick.current = true;
    drag.current = null;
  };
  const onClick = (event: ReactMouseEvent) => {
    if (suppressClick.current) {
      suppressClick.current = false;
      return;
    }
    const hit = (event.target as HTMLElement).closest<HTMLElement>("[data-node]");
    onSelect(hit ? hit.dataset.node! : null);
  };

  const zoomBy = (factor: number) => {
    const el = viewport.current;
    if (!el) return;
    const px = el.clientWidth / 2;
    const py = el.clientHeight / 2;
    setView((v) => {
      const k = clamp(v.k * factor, 0.1, 2.5);
      return { k, x: px - (px - v.x) * (k / v.k), y: py - (py - v.y) * (k / v.k) };
    });
  };

  const frames = placed.nodes.filter((p) => FRAME_KINDS.has(p.node.kind)).sort((a, b) => a.depth - b.depth);
  const leaves = placed.nodes.filter((p) => !FRAME_KINDS.has(p.node.kind));
  const hot = (id: string) => selected !== null && (id === selected);

  return (
    <div
      className="viewport"
      ref={viewport}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onClick={onClick}
    >
      <div className="world" style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})` }}>
        {frames.map((p) => (
          <FrameBox key={p.node.id} p={p} selected={hot(p.node.id)} onToggle={onToggle} following={following} />
        ))}
        <svg className="edges" width={placed.width} height={placed.height} key={placed.edges.length + ":" + placed.width + ":" + placed.height}>
          <defs>
            <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0,0 L10,5 L0,10 z" className="arrow" />
            </marker>
            <marker id="arrow-error" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0,0 L10,5 L0,10 z" className="arrow arrow-error" />
            </marker>
          </defs>
          {placed.edges.map((e) => (
            <path
              key={e.id}
              d={roundedPath(e.points)}
              className={cx("edge", `edge-${e.edge.kind}`, (hot(e.edge.source) || hot(e.edge.target)) && "is-hot")}
              markerEnd={e.edge.kind === "error" ? "url(#arrow-error)" : "url(#arrow)"}
            />
          ))}
        </svg>
        {placed.edges.map(
          (e) =>
            e.label && (
              <div
                key={e.id}
                className={cx("edge-label", `edge-label-${e.edge.kind}`, onFollow && e.edge.data.length > 0 && "is-data")}
                style={{ left: e.label.x, top: e.label.y, width: e.label.width, height: e.label.height }}
                title={onFollow && e.edge.data.length ? "Click a name to follow that data through the code" : e.label.text}
              >
                {onFollow && e.edge.data.length
                  ? e.edge.data.map((name, i) => (
                      <span key={name}>
                        {i > 0 && ", "}
                        <button
                          className="data-chip"
                          onPointerDown={(event) => event.stopPropagation()}
                          onClick={(event) => {
                            event.stopPropagation();
                            onFollow(e.edge.source, name);
                          }}
                          title={`Follow ${name.startsWith("$") ? "this result" : name} through the code`}
                        >
                          {name.startsWith("$") ? "result" : name}
                        </button>
                      </span>
                    ))
                  : e.label.text}
              </div>
            ),
        )}
        {leaves.map((p) => (
          <NodeBox key={p.node.id} p={p} selected={hot(p.node.id)} onToggle={onToggle} following={following} />
        ))}
      </div>
      <div className="zoom-controls" onPointerDown={(e) => e.stopPropagation()} onClick={(e) => e.stopPropagation()}>
        <button onClick={() => zoomBy(1.2)} title="Zoom in (ctrl + wheel)">+</button>
        <button onClick={() => zoomBy(1 / 1.2)} title="Zoom out">−</button>
        <button onClick={fit} title="Fit (F)">Fit</button>
        <span className="zoom-level">{Math.round(view.k * 100)}%</span>
      </div>
    </div>
  );
}

function tooltip(node: FlowNode): string {
  const d = node.detail;
  const parts = [node.label];
  if (d.text && d.text !== node.label) parts.push(d.text);
  if (d.file) parts.push(d.file);
  if (d.doc) parts.push(d.doc);
  return parts.join("\n");
}

interface BoxProps {
  p: PlacedNode;
  selected: boolean;
  onToggle: (node: FlowNode) => void;
  following: boolean;
}

function ToggleButton({ node, open, onToggle }: { node: FlowNode; open: boolean; onToggle: (node: FlowNode) => void }) {
  return (
    <button
      className="toggle"
      onPointerDown={(e) => e.stopPropagation()}
      onClick={(e) => {
        e.stopPropagation();
        onToggle(node);
      }}
      title={open ? "Close this step" : "Open this step here"}
      aria-label={open ? "close" : "open"}
    >
      {open ? "−" : "+"}
    </button>
  );
}

function NodeBox({ p, selected, onToggle, following }: BoxProps) {
  const { node } = p;
  const d = node.detail;
  const style = { left: p.x, top: p.y, width: p.width, height: p.height };
  const className = cx(
    "node",
    `node-${node.kind}`,
    selected && "is-selected",
    d.inner && "is-inner",
    d.confidence === "guess" && "is-guess",
    d.trail === "origin" && "is-origin",
  );
  const common = { className, style, "data-node": node.id, title: tooltip(node) };

  switch (node.kind) {
    case "start":
    case "end":
      return (
        <div {...common}>
          <span className="terminal-text">{node.label}</span>
        </div>
      );
    case "step":
      return (
        <div {...common} onDoubleClick={() => d.expandable && !following && onToggle(node)}>
          <div className="step-head">
            <span className="step-name">{node.label}</span>
            {d.recursive && <span className="step-badge" title="Calls a function that is already open above">↻</span>}
            {d.returns && (
              <span className="step-badge step-returns" title="Hands the data back to its caller">
                ↩
              </span>
            )}
            {d.expandable && !following && <ToggleButton node={node} open={false} onToggle={onToggle} />}
          </div>
          {d.receives ? (
            <div className="step-file">receives {d.receives.join(", ") || "it from its object"}</div>
          ) : (
            <div className="step-file">{d.file}</div>
          )}
          {d.doc && <div className="step-doc">{d.doc}</div>}
        </div>
      );
    case "io": {
      const [channel, direction] = d.io ?? ["", ""];
      return (
        <div {...common}>
          <svg className="shape" width={p.width} height={p.height}>
            <polygon points={`14,1 ${p.width - 1},1 ${p.width - 14},${p.height - 1} 1,${p.height - 1}`} />
          </svg>
          <div className="io-tag">
            {IO_CHANNEL[channel] ?? channel} · {IO_DIRECTION[direction] ?? direction}
          </div>
          <code className="io-text">{node.label}</code>
        </div>
      );
    }
    case "use":
      return (
        <div {...common}>
          <code className="use-text">{node.label}</code>
        </div>
      );
    case "decision":
      return (
        <div {...common}>
          <svg className="shape" width={p.width} height={p.height}>
            <polygon points={`${p.width / 2},1 ${p.width - 1},${p.height / 2} ${p.width / 2},${p.height - 1} 1,${p.height / 2}`} />
          </svg>
          <code className="decision-text">{node.label}</code>
        </div>
      );
    case "raise":
      return (
        <div {...common}>
          <span className="raise-icon" aria-hidden>
            ⚠
          </span>
          <span>{node.label}</span>
        </div>
      );
    default:
      return null;
  }
}

function FrameBox({ p, selected, onToggle, following }: BoxProps) {
  const { node } = p;
  const style = { left: p.x, top: p.y, width: p.width, height: p.height };
  const title =
    node.kind === "loop"
      ? `↻ ${node.label}`
      : node.kind === "handler"
        ? `⚠ ${node.label}`
        : node.kind === "file"
          ? `▣ ${node.label}`
          : node.label;
  return (
    <div className={cx("frame", `frame-${node.kind}`, selected && "is-selected")} style={style} data-node={node.id}>
      <div className="frame-head" title={tooltip(node)}>
        {node.kind === "group" && !following && <ToggleButton node={node} open onToggle={onToggle} />}
        <span className="frame-title">{title}</span>
        {node.kind === "group" && <span className="frame-file">{node.detail.file}</span>}
      </div>
    </div>
  );
}
