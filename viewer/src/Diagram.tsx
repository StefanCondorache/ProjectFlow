import { useCallback, useEffect, useLayoutEffect, useRef, useState, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from "react";
import { FRAME_KINDS, roundedPath, type Direction, type Placed, type PlacedNode, type Point } from "./layout";
import { pathLength, pointAt, tokenPath, type SimFrame } from "./sim";
import type { FlowNode } from "./types";

/** A running simulation, as the diagram needs it. */
export interface TokenRun {
  key: string; // changes on every move
  first: boolean; // the run just started: bring the camera in close
  frame: SimFrame;
  previous: SimFrame | null;
  speed: number;
  badge: string;
  walked: Set<string>; // "from>to" of the arrows travelled so far
  onArrive: () => void;
}

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
  /** Which way the flow runs; the view frames it accordingly. */
  direction: Direction;
  /** Start following a piece of data from a node (absent: data labels are plain text). */
  onFollow?: (at: string, variable: string) => void;
  /** The diagram shows a data trail: steps cannot be opened or closed by hand. */
  following: boolean;
  /** When set, a token carries the data along the diagram. */
  run?: TokenRun | null;
}

const clamp = (value: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, value));
const cx = (...names: (string | false | null | undefined)[]) => names.filter(Boolean).join(" ");

const IO_CHANNEL: Record<string, string> = { file: "file", db: "database", net: "network", env: "env", process: "process", console: "console" };
const IO_DIRECTION: Record<string, string> = { in: "in", out: "out", inout: "in/out" };

export function Diagram({ placed, rootKey, anchor, fitSignal, selected, onSelect, onToggle, direction, onFollow, following, run }: Props) {
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
    const start = placed.nodes.find((p) => p.node.kind === "start" && !p.node.parent);
    if (direction === "RIGHT") {
      // from the left edge, centred on START
      const k = clamp((h - 48) / placed.height, 0.6, 1);
      const middle = start ? start.y + start.height / 2 : placed.height / 2;
      setView({ k, x: 24 - (start ? start.x - 24 : 0) * k, y: h / 2 - middle * k });
    } else {
      // from the top, centred on START
      const k = clamp((w - 48) / placed.width, 0.6, 1);
      const centre = start ? start.x + start.width / 2 : placed.width / 2;
      setView({ k, x: w / 2 - centre * k, y: 24 });
    }
  }, [placed, direction]);

  // New root: opening view. Same root, new layout: keep the step that was
  // opened or closed where it was on screen (its left-middle when the flow
  // runs left to right, its top-centre when it runs down), zooming out only
  // if it no longer fits.
  useLayoutEffect(() => {
    const before = previous.current;
    const boxes = new Map(placed.nodes.map((p) => [p.node.id, p]));
    if (!before || before.rootKey !== rootKey) {
      openingView();
    } else if (anchor) {
      const was = before.boxes.get(anchor);
      const now = boxes.get(anchor);
      const el = viewport.current;
      if (was && now && el) {
        setView((v) => {
          if (direction === "RIGHT") {
            const sx = v.x + was.x * v.k;
            const sy = v.y + (was.y + was.height / 2) * v.k;
            const room = el.clientHeight - 48;
            const k = now.height * v.k > room ? clamp(room / now.height, 0.35, v.k) : v.k;
            return { k, x: sx - now.x * k, y: sy - (now.y + now.height / 2) * k };
          }
          const sx = v.x + (was.x + was.width / 2) * v.k;
          const sy = v.y + was.y * v.k;
          const room = el.clientWidth - 48;
          const k = now.width * v.k > room ? clamp(room / now.width, 0.35, v.k) : v.k;
          return { k, x: sx - (now.x + now.width / 2) * k, y: sy - now.y * k };
        });
      }
    }
    previous.current = { rootKey, boxes };
  }, [placed, rootKey, anchor, openingView, direction]);

  useEffect(() => {
    if (fitSignal) fit();
  }, [fitSignal, fit]);

  // Keep the selected node in view, also when a side panel opens or is dragged wider.
  const [size, setSize] = useState({ w: 0, h: 0 });
  useEffect(() => {
    const el = viewport.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const box = placed.nodes.find((p) => p.node.id === selected);
    if (!box || !size.w) return;
    setView((v) => {
      const left = v.x + box.x * v.k;
      const right = v.x + (box.x + box.width) * v.k;
      if (right > size.w - 24) return { ...v, x: v.x - Math.min(right - (size.w - 24), left - 24) };
      if (left < 24) return { ...v, x: v.x + (24 - left) };
      return v;
    });
  }, [selected, size.w, placed]);

  // The simulation token: each new frame moves it along its route, then reports arrival.
  const [token, setToken] = useState<{ x: number; y: number; angle: number } | null>(null);
  const [gliding, setGliding] = useState(false);
  const tokenAt = useRef<Point | null>(null);
  const arrive = useRef<() => void>(() => undefined);
  arrive.current = run?.onArrive ?? (() => undefined);
  const runKey = run?.key ?? null;
  useEffect(() => {
    if (!run) {
      setToken(null);
      tokenAt.current = null;
      return;
    }
    // A frame in a box that is still being laid out (just opened) waits for it.
    if (!placed.nodes.some((p) => p.node.id === run.frame.node)) return;
    const points = tokenPath(placed, run.frame, run.previous, tokenAt.current, direction);
    if (points.length === 0) {
      arrive.current();
      return;
    }
    // Keep the token where it can be read: close up when the run starts, then
    // glide along whenever it heads for the edge of the view.
    const end = points[points.length - 1];
    const el = viewport.current;
    if (el) {
      setView((v) => {
        const k = run.first ? Math.max(v.k, 0.9) : v.k;
        const sx = v.x + end.x * k;
        const sy = v.y + end.y * k;
        const mx = el.clientWidth * 0.2;
        const my = el.clientHeight * 0.2;
        const inView = sx > mx && sx < el.clientWidth - mx && sy > my && sy < el.clientHeight - my;
        if (!run.first && inView) return v;
        setGliding(true);
        window.setTimeout(() => setGliding(false), 500);
        return { k, x: el.clientWidth / 2 - end.x * k, y: el.clientHeight / 2 - end.y * k };
      });
    }
    const length = pathLength(points);
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const duration = still ? 0 : Math.min(1400, Math.max(280, length * 2.4)) / run.speed;
    const started = performance.now();
    let raf = 0;
    const tick = (now: number) => {
      const t = duration ? Math.min(1, (now - started) / duration) : 1;
      const eased = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
      setToken(pointAt(points, eased * length));
      if (t < 1) {
        raf = requestAnimationFrame(tick);
      } else {
        tokenAt.current = end;
        arrive.current();
      }
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runKey, placed, direction]);

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

  const [hovered, setHovered] = useState<string | null>(null);
  const frames = placed.nodes.filter((p) => FRAME_KINDS.has(p.node.kind)).sort((a, b) => a.depth - b.depth);
  const leaves = placed.nodes.filter((p) => !FRAME_KINDS.has(p.node.kind));
  const hot = (id: string) => id === selected || id === hovered;

  return (
    <div
      className="viewport"
      ref={viewport}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onClick={onClick}
      onPointerOver={(event) => {
        const hit = (event.target as HTMLElement).closest<HTMLElement>("[data-node]");
        setHovered(hit && !FRAME_KINDS.has(hit.dataset.kind ?? "") ? hit.dataset.node! : null);
      }}
      onPointerLeave={() => setHovered(null)}
    >
      <div
        className={cx("world", gliding && "is-gliding")}
        style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})` }}
      >
        {frames.map((p) => (
          <FrameBox
            key={p.node.id}
            p={p}
            selected={p.node.id === selected}
            current={run?.frame.node === p.node.id}
            failing={run?.frame.node === p.node.id && Boolean(run.frame.error)}
            onToggle={onToggle}
            following={following}
          />
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
              className={cx(
                "edge",
                `edge-${e.edge.kind}`,
                (hot(e.edge.source) || hot(e.edge.target)) && "is-hot",
                run?.walked.has(`${e.edge.source}>${e.edge.target}`) && "is-walked",
              )}
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
          <NodeBox
            key={p.node.id}
            p={p}
            selected={p.node.id === selected}
            current={run?.frame.node === p.node.id}
            failing={run?.frame.node === p.node.id && Boolean(run.frame.error)}
            onToggle={onToggle}
            following={following}
          />
        ))}
        {run && token && (
          <>
            <svg className={cx("token-layer", run.frame.error && "is-failing")} width={placed.width} height={placed.height} aria-hidden>
              <g transform={`translate(${token.x} ${token.y})`}>
                <circle r={13} className="token-halo" />
                <circle r={8} className="token-core" />
                <path d="M -4 -5 L 6 0 L -4 5 Z" className="token-arrow" transform={`rotate(${token.angle})`} />
              </g>
            </svg>
            <div className={cx("token-badge", run.frame.error && "is-failing")} style={{ left: token.x + 14, top: token.y - 34 }}>
              {run.badge}
            </div>
          </>
        )}
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
  current?: boolean;
  /** The data is raising an error here. */
  failing?: boolean;
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

function NodeBox({ p, selected, current, failing, onToggle, following }: BoxProps) {
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
    current && "is-current",
    failing && "is-failing",
  );
  const common = { className, style, "data-node": node.id, "data-kind": node.kind, title: tooltip(node) };

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

function FrameBox({ p, selected, current, failing, onToggle, following }: BoxProps) {
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
    <div
      className={cx("frame", `frame-${node.kind}`, selected && "is-selected", current && "is-current", failing && "is-failing")}
      style={style}
      data-node={node.id}
      data-kind={node.kind}
    >
      <div className="frame-head" title={tooltip(node)}>
        {node.kind === "group" && !following && <ToggleButton node={node} open onToggle={onToggle} />}
        <span className="frame-title">{title}</span>
        {node.kind === "group" && <span className="frame-path">{node.detail.file}</span>}
      </div>
    </div>
  );
}
