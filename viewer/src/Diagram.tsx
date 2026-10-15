import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from "react";
import { isFrame, roundedPath, type Direction, type Placed, type PlacedNode, type Point } from "./layout";
import { overviewPoint, overviewTransform, resizeView, revealBox, visibleGraphWindow, type CameraView as View } from "./minimap";
import { pathLength, pointAt, tokenPath, type SimFrame } from "./sim";
import type { FlowEdge, FlowNode } from "./types";
import { edgeDataLabel } from "./data-labels";
import { languageName } from "./api";

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
  /** Structural map presentation, with a neutral opening view and dependency emphasis. */
  map?: boolean;
}

const clamp = (value: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, value));
const cx = (...names: (string | false | null | undefined)[]) => names.filter(Boolean).join(" ");
const OVERVIEW_SIZE = { width: 176, height: 110 };
const OVERVIEW_PAN: Record<string, [number, number]> = { ArrowLeft: [1, 0], ArrowRight: [-1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] };
const isolateControlKey = (event: ReactKeyboardEvent) => {
  if (event.key !== "Escape") event.stopPropagation();
};

const IO_CHANNEL: Record<string, string> = { file: "file", db: "database", net: "network", env: "env", process: "process", console: "console" };
const IO_DIRECTION: Record<string, string> = { in: "in", out: "out", inout: "in/out" };

function dependencyDescription(edge: FlowEdge): string {
  const confidence = edge.confidence === "inferred" ? "Inferred dependency" : edge.confidence === "verified" ? "Verified dependency" : "Dependency";
  const count = edge.count ?? 1;
  return [`${confidence} · ${count} import${count === 1 ? "" : "s"}`, ...(edge.evidence ?? []).map((item) => `${item.file}:${item.line} — ${item.text}`)].join("\n");
}

export function Diagram({ placed, rootKey, anchor, fitSignal, selected, onSelect, onToggle, direction, onFollow, following, run, map = false }: Props) {
  const viewport = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<View>({ x: 0, y: 0, k: 1 });
  const previous = useRef<{ rootKey: string; boxes: Map<string, PlacedNode> } | null>(null);
  const previousFitSignal = useRef(0);

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
    if (map) {
      const whole = Math.min((w - 80) / placed.width, (h - 80) / placed.height, 1);
      if (whole >= 0.6) {
        setView({ k: whole, x: (w - placed.width * whole) / 2, y: (h - placed.height * whole) / 2 });
      } else {
        const first = [...placed.nodes].sort((a, b) => a.y - b.y || a.x - b.x)[0];
        // Keep the first card or folder heading readable; Fit still shows the
        // complete map when the user wants its wider structure.
        setView({ k: 0.6, x: 24 - (first?.x ?? 0) * 0.6, y: 24 - (first?.y ?? 0) * 0.6 });
      }
      return;
    }
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
  }, [placed, direction, map]);

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
          if (map) {
            // Folder headings stay where they were opened, at the readable
            // zoom the user chose, regardless of the frame's new dimensions.
            return { k: v.k, x: v.x + (was.x - now.x) * v.k, y: v.y + (was.y - now.y) * v.k };
          }
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
  }, [placed, rootKey, anchor, openingView, direction, map]);

  useEffect(() => {
    if (previousFitSignal.current === fitSignal) return;
    previousFitSignal.current = fitSignal;
    if (fitSignal) fit();
  }, [fitSignal, fit]);

  // Keep the selected node in view, also when a side panel opens or is dragged wider.
  const [size, setSize] = useState({ w: 0, h: 0 });
  const previousSize = useRef({ width: 0, height: 0 });
  useEffect(() => {
    const el = viewport.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(() => {
    const before = previousSize.current;
    const after = { width: size.w, height: size.h };
    previousSize.current = after;
    setView((v) => resizeView(v, before, after));
  }, [size.w, size.h]);
  useEffect(() => {
    const box = placed.nodes.find((p) => p.node.id === selected);
    if (!box || !size.w || !size.h) return;
    setView((v) => revealBox(v, { width: size.w, height: size.h }, box));
  }, [selected, size.w, size.h, placed]);

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
    if (event.button === 0) {
      suppressClick.current = false;
      drag.current = { x: event.clientX, y: event.clientY, moved: false, pointer: event.pointerId };
    }
  };
  const onPointerMove = (event: ReactPointerEvent) => {
    const d = drag.current;
    if (!d || d.pointer !== event.pointerId) return;
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
  const onPointerUp = (event: ReactPointerEvent) => {
    const d = drag.current;
    if (!d || d.pointer !== event.pointerId) return;
    if (d.moved) suppressClick.current = true;
    drag.current = null;
    if (viewport.current?.hasPointerCapture(d.pointer)) viewport.current.releasePointerCapture(d.pointer);
  };
  const onPointerCancel = () => {
    drag.current = null;
    suppressClick.current = false;
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

  const recenter = (point: Point) => {
    const el = viewport.current;
    if (!el) return;
    setView((v) => ({ ...v, x: el.clientWidth / 2 - point.x * v.k, y: el.clientHeight / 2 - point.y * v.k }));
  };

  const revealFocused = (box: PlacedNode) => {
    const el = viewport.current;
    if (!el) return;
    const bounds = { width: el.clientWidth, height: el.clientHeight };
    setView((v) => {
      // A frame's keyboard target is its heading, even when the frame itself is larger than the screen.
      const target = isFrame(box.node)
        ? { ...box, width: Math.min(box.width, Math.max(0, bounds.width - 48) / v.k), height: 30 }
        : box;
      return revealBox(v, bounds, target);
    });
  };

  const overview = overviewTransform(placed, OVERVIEW_SIZE);
  const visibleWindow = visibleGraphWindow(view, { width: size.w, height: size.h }, placed);

  const [hovered, setHovered] = useState<string | null>(null);
  const frames = placed.nodes.filter((p) => isFrame(p.node)).sort((a, b) => a.depth - b.depth);
  const leaves = placed.nodes.filter((p) => !isFrame(p.node));
  const hot = (id: string) => id === selected || id === hovered;
  const mapHighlight = map && Boolean(selected || hovered);
  const connected = (source: string, target: string) => hot(source) || hot(target);

  return (
    <div
      className={cx("viewport", map && "map-viewport")}
      ref={viewport}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
      onLostPointerCapture={() => { drag.current = null; }}
      onClick={onClick}
      onPointerOver={(event) => {
        const hit = (event.target as HTMLElement).closest<HTMLElement>("[data-node]");
        const node = hit && placed.nodes.find((p) => p.node.id === hit.dataset.node)?.node;
        setHovered(node && (map || !isFrame(node)) ? node.id : null);
      }}
      onPointerLeave={() => setHovered(null)}
      onFocusCapture={(event) => {
        if (!map) return;
        const hit = (event.target as HTMLElement).closest<HTMLElement>("[data-node]");
        setHovered(hit?.dataset.node ?? null);
      }}
      onBlurCapture={(event) => {
        if (map && !event.currentTarget.contains(event.relatedTarget as Node | null)) setHovered(null);
      }}
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
            onFocus={revealFocused}
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
            <marker id="arrow-connected" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0,0 L10,5 L0,10 z" className="arrow arrow-connected" />
            </marker>
          </defs>
          {placed.edges.map((e) => (
            <path
              key={e.id}
              d={roundedPath(e.points)}
              className={cx(
                "edge",
                `edge-${e.edge.kind}`,
                e.edge.kind === "dependency" && e.edge.confidence === "inferred" && "is-inferred",
                connected(e.edge.source, e.edge.target) && "is-hot",
                mapHighlight && !connected(e.edge.source, e.edge.target) && "is-muted",
                run?.walked.has(`${e.edge.source}>${e.edge.target}`) && "is-walked",
              )}
              markerEnd={e.edge.kind === "error" ? "url(#arrow-error)" : map && connected(e.edge.source, e.edge.target) ? "url(#arrow-connected)" : "url(#arrow)"}
              role={e.edge.kind === "dependency" ? "img" : undefined}
              aria-label={e.edge.kind === "dependency" ? dependencyDescription(e.edge) : undefined}
            >
              {e.edge.kind === "dependency" && <title>{dependencyDescription(e.edge)}</title>}
            </path>
          ))}
        </svg>
        {placed.edges.map(
          (e) =>
            e.label && (
              <div
                key={e.id}
                className={cx("edge-label", `edge-label-${e.edge.kind}`, onFollow && e.edge.data.length > 0 && "is-data", map && connected(e.edge.source, e.edge.target) && "is-hot", mapHighlight && !connected(e.edge.source, e.edge.target) && "is-muted")}
                style={{ left: e.label.x, top: e.label.y, width: e.label.width, height: e.label.height }}
                title={e.edge.kind === "dependency" ? dependencyDescription(e.edge) : onFollow && e.edge.data.length ? "Click a name to follow that data through the code" : e.label.text}
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
                          title={`Follow ${edgeDataLabel(name, e.edge.generated_data, "this result")} through the code`}
                        >
                          {edgeDataLabel(name, e.edge.generated_data)}
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
            onFocus={revealFocused}
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
      <div className="zoom-controls" role="group" aria-label="Diagram zoom" onPointerDown={(e) => e.stopPropagation()} onClick={(e) => e.stopPropagation()} onKeyDown={isolateControlKey}>
        <button onClick={() => zoomBy(1.2)} aria-label="Zoom in" title="Zoom in (ctrl + wheel)">+</button>
        <button onClick={() => zoomBy(1 / 1.2)} aria-label="Zoom out" title="Zoom out">−</button>
        <button onClick={fit} aria-label="Fit entire diagram" title="Fit (F)">Fit</button>
        <button className="zoom-level" onClick={() => zoomBy(1 / view.k)} aria-label={`Reset zoom to 100 percent; current zoom ${Math.round(view.k * 100)} percent`} title="Reset zoom to 100%">{Math.round(view.k * 100)}%</button>
      </div>
      <div className="minimap" onPointerDown={(e) => e.stopPropagation()} onPointerMove={(e) => e.stopPropagation()} onPointerUp={(e) => e.stopPropagation()} onPointerCancel={(e) => e.stopPropagation()} onClick={(e) => e.stopPropagation()}>
        <div className="minimap-header"><span>Overview</span><span aria-hidden>{placed.nodes.length} {map ? "areas & files" : "nodes"}</span></div>
        <svg
          className="minimap-map"
          width={OVERVIEW_SIZE.width}
          height={OVERVIEW_SIZE.height}
          viewBox={`0 0 ${OVERVIEW_SIZE.width} ${OVERVIEW_SIZE.height}`}
          role="button"
          tabIndex={0}
          aria-label="Diagram overview. Click to recenter; use arrow keys to pan. Enter centers the diagram."
          onClick={(event) => {
            event.stopPropagation();
            const rect = event.currentTarget.getBoundingClientRect();
            recenter(overviewPoint({ x: (event.clientX - rect.left) * OVERVIEW_SIZE.width / rect.width, y: (event.clientY - rect.top) * OVERVIEW_SIZE.height / rect.height }, overview, placed));
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") return;
            event.stopPropagation();
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              recenter({ x: placed.width / 2, y: placed.height / 2 });
              return;
            }
            const pan = OVERVIEW_PAN[event.key];
            if (!pan) return;
            event.preventDefault();
            const step = event.shiftKey ? 160 : 80;
            setView((v) => ({ ...v, x: v.x + pan[0] * step, y: v.y + pan[1] * step }));
          }}
        >
          <g transform={`translate(${overview.x} ${overview.y}) scale(${overview.scale})`} aria-hidden>
            {frames.map((p) => <rect key={p.node.id} className={cx("minimap-node", `minimap-node-${p.node.kind}`, "is-frame", p.node.id === selected && "is-selected")} x={p.x} y={p.y} width={p.width} height={p.height} rx={6 / overview.scale} />)}
            {placed.edges.map((edge) => <path key={edge.id} className={cx("minimap-edge", `minimap-edge-${edge.edge.kind}`, map && edge.edge.confidence === "inferred" && "is-inferred", mapHighlight && !connected(edge.edge.source, edge.edge.target) && "is-muted")} d={roundedPath(edge.points)} />)}
            {leaves.map((p) => <rect key={p.node.id} className={cx("minimap-node", `minimap-node-${p.node.kind}`, p.node.id === selected && "is-selected")} x={p.x} y={p.y} width={p.width} height={p.height} rx={Math.min(3 / overview.scale, p.height / 2)} />)}
            <rect className="minimap-window" x={visibleWindow.x} y={visibleWindow.y} width={visibleWindow.width} height={visibleWindow.height} rx={2 / overview.scale} />
          </g>
        </svg>
      </div>
    </div>
  );
}

function tooltip(node: FlowNode): string {
  const d = node.detail;
  const parts = [node.label];
  if (d.text && d.text !== node.label) parts.push(d.text);
  if (d.file) parts.push(d.file);
  if (d.path) parts.push(d.path);
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
  onFocus: (box: PlacedNode) => void;
  following: boolean;
}

function ToggleButton({ node, open, onToggle }: { node: FlowNode; open: boolean; onToggle: (node: FlowNode) => void }) {
  return (
    <button
      className="toggle"
      onPointerDown={(e) => e.stopPropagation()}
      onKeyDown={isolateControlKey}
      onClick={(e) => {
        e.stopPropagation();
        onToggle(node);
      }}
      title={node.kind === "folder" ? open ? "Collapse this folder" : "Expand this folder here" : open ? "Close this step" : "Open this step here"}
      aria-label={`${node.kind === "folder" ? open ? "Collapse" : "Expand" : open ? "Close" : "Open"} ${node.label}`}
      aria-expanded={open}
    >
      {open ? "−" : "+"}
    </button>
  );
}

function NodeBox({ p, selected, current, failing, onToggle, onFocus, following }: BoxProps) {
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
  const selector = <button className="node-select" aria-label={`Select ${node.label}`} aria-pressed={selected} onFocus={() => onFocus(p)} onKeyDown={isolateControlKey} />;

  switch (node.kind) {
    case "folder":
    case "module":
    case "config":
    case "document":
    case "asset":
    case "external": {
      const basename = node.kind === "external" ? node.label : (d.path ?? node.label).split("/").filter(Boolean).pop() || node.label;
      const badge = node.kind === "folder" ? "Folder" : (d.language && languageName(d.language)) || d.category || (node.kind === "module" ? "File" : node.kind);
      const metadata = node.kind === "folder" ? `${d.files ?? 0} file${d.files === 1 ? "" : "s"}` : node.kind === "external" ? "External dependency" : d.functions?.length ? `${d.functions.length} function${d.functions.length === 1 ? "" : "s"}` : d.size !== undefined ? formatBytes(d.size) : "Project file";
      return (
        <div {...common} className={cx(className, "map-card")} onDoubleClick={() => node.kind === "folder" && d.expandable && onToggle(node)}>
          {selector}
          <div className="map-card-main">
            <MapIllustration kind={node.kind} />
            <div className="map-card-copy">
              <span className="map-card-title">{basename}</span>
              <span className="map-card-type">{badge}</span>
            </div>
            {node.kind === "folder" && d.expandable && <ToggleButton node={node} open={false} onToggle={onToggle} />}
          </div>
          <div className="map-card-footer"><span>{metadata}</span><span>{node.kind === "folder" ? "Explore area" : d.analysis === "semantic" ? d.functions?.length ? "Flow available" : "Parsed source" : d.analysis === "partial" ? "Partial analysis" : d.analysis === "imports" || d.analysis === "inferred" ? "Import analysis" : "Structure"}</span></div>
        </div>
      );
    }
    case "start":
    case "end":
      return (
        <div {...common}>
          {selector}
          <span className="terminal-text">{node.label}</span>
        </div>
      );
    case "step":
      return (
        <div {...common} onDoubleClick={() => d.expandable && !following && onToggle(node)}>
          {selector}
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
          {selector}
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
          {selector}
          <code className="use-text">{node.label}</code>
        </div>
      );
    case "decision":
      return (
        <div {...common}>
          {selector}
          <svg className="shape" width={p.width} height={p.height}>
            <polygon points={`${p.width / 2},1 ${p.width - 1},${p.height / 2} ${p.width / 2},${p.height - 1} 1,${p.height / 2}`} />
          </svg>
          <code className="decision-text">{node.label}</code>
        </div>
      );
    case "raise":
      return (
        <div {...common}>
          {selector}
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

function FrameBox({ p, selected, current, failing, onToggle, onFocus, following }: BoxProps) {
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
        {(node.kind === "group" && !following || node.kind === "folder" && node.detail.expandable) && <ToggleButton node={node} open onToggle={onToggle} />}
        <button className="frame-select" aria-label={`Select ${node.label}`} aria-pressed={selected} onFocus={() => onFocus(p)} onKeyDown={isolateControlKey}>
          {node.kind === "folder" && <MapIllustration kind="folder" small />}
          <span className="frame-title">{title}</span>
          {node.kind === "group" && <span className="frame-path">{node.detail.file}</span>}
          {node.kind === "folder" && <span className="frame-path">{node.detail.files ?? 0} files</span>}
        </button>
      </div>
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Native vector illustrations share the map's category colors in both themes. */
function MapIllustration({ kind, small = false }: { kind: FlowNode["kind"]; small?: boolean }) {
  const contents = kind === "folder" ? <>
    <path className="illustration-fill" d="M5 11a3 3 0 0 1 3-3h11l4 4h14a3 3 0 0 1 3 3v19a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3Z" />
    <path d="M5 17h35M14 24h9m-9 5h16" />
  </> : kind === "external" ? <>
    <path className="illustration-fill" d="m22 6 17 9v17l-17 9-17-9V15Z" />
    <path d="m5 15 17 9 17-9M22 24v17m-8-31 17 9" />
  </> : <>
    <path className="illustration-fill" d="M11 5h17l9 9v24a3 3 0 0 1-3 3H11a3 3 0 0 1-3-3V8a3 3 0 0 1 3-3Z" />
    <path d="M28 5v9h9" />
    {kind === "module" ? <path d="m17 23-5 5 5 5m11-10 5 5-5 5m-4-13-4 16" /> : kind === "config" ? <><path d="M15 22h15m-15 7h15m-15 7h15" /><circle cx="20" cy="22" r="2" className="illustration-fill" /><circle cx="26" cy="29" r="2" className="illustration-fill" /><circle cx="19" cy="36" r="2" className="illustration-fill" /></> : kind === "asset" ? <><rect x="14" y="21" width="17" height="14" rx="2" /><path d="m14 32 5-6 4 4 3-3 5 6" /><circle cx="27" cy="24" r="1" /></> : <path d="M15 22h15m-15 6h15m-15 6h10" />}
  </>;
  return <svg className={cx("map-illustration", `map-illustration-${kind}`, small && "is-small")} viewBox="0 0 44 46" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{contents}</svg>;
}
