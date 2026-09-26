import { useCallback, useEffect, useRef, useState } from "react";
import { api, readHash, writeHash, type ViewState } from "./api";
import { Details } from "./Details";
import { Diagram, type TokenRun } from "./Diagram";
import { layoutGraph } from "./elk";
import type { Direction, Placed } from "./layout";
import { Resizer } from "./Resizer";
import { Sidebar } from "./Sidebar";
import { badgeFor, type Simulation } from "./sim";
import { SimPanel } from "./SimPanel";
import type { Entry, FlowGraph, FlowNode, ProjectInfo } from "./types";

interface SimState {
  run: number; // bumps on every (re)start, so the token restarts
  data: Simulation;
  index: number;
  playing: boolean;
  choices: Record<string, string>;
}

const LEFT = { initial: 290, min: 200, max: 560 };
const RIGHT = { initial: 400, min: 300, max: 800 };
const clamp = (value: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, value));

function stored<T extends string | number>(key: string, fallback: T): T {
  const value = localStorage.getItem(`flowmap.${key}`);
  if (value === null) return fallback;
  return (typeof fallback === "number" ? Number(value) : value) as T;
}

export function App() {
  const [project, setProject] = useState<ProjectInfo | null>(null);
  const [view, setView] = useState<ViewState | null>(null);
  const [graph, setGraph] = useState<FlowGraph | null>(null);
  const [placed, setPlaced] = useState<Placed | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [anchor, setAnchor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [fitSignal, setFitSignal] = useState(0);
  const [toast, setToast] = useState<string | null>(null);
  const [direction, setDirection] = useState<Direction>(() => stored<Direction>("direction", "RIGHT"));
  const [leftWidth, setLeftWidth] = useState(() => stored("left", LEFT.initial));
  const [rightWidth, setRightWidth] = useState(() => stored("right", RIGHT.initial));
  const autoOpened = useRef(0);
  // Bumped by automatic opens so the diagram re-frames as for a new root.
  const [opening, setOpening] = useState(0);
  const [sim, setSim] = useState<SimState | null>(null);
  const [simBusy, setSimBusy] = useState(false);
  const [speed, setSpeed] = useState<number>(() => stored<number>("speed", 1));
  const [tab, setTab] = useState<"sim" | "details">("details");
  const [tips, setTips] = useState(() => stored("tips", "on") === "on");
  const dwell = useRef<number | undefined>(undefined);

  useEffect(() => localStorage.setItem("flowmap.direction", direction), [direction]);
  useEffect(() => localStorage.setItem("flowmap.left", String(leftWidth)), [leftWidth]);
  useEffect(() => localStorage.setItem("flowmap.right", String(rightWidth)), [rightWidth]);
  useEffect(() => localStorage.setItem("flowmap.speed", String(speed)), [speed]);

  // A simulation belongs to the diagram it was started on.
  useEffect(() => {
    window.clearTimeout(dwell.current);
    setSim(null);
  }, [view]);

  const go = useCallback((next: ViewState, mode: "push" | "replace", anchorId: string | null = null) => {
    writeHash(next, mode);
    setAnchor(anchorId);
    setView(next);
  }, []);

  useEffect(() => {
    api
      .project()
      .then((p) => {
        setProject(p);
        const fromUrl = readHash();
        if (fromUrl) setView(fromUrl);
        else if (p.entries.length) go({ root: p.entries[0].target, start: p.entries[0].label, expanded: [] }, "replace");
      })
      .catch((e) => setError(String(e)));
    const onPop = () => {
      const state = readHash();
      if (state) {
        setAnchor(null);
        setSelected(null);
        setView(state);
      }
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [go]);

  // Fetch the diagram for the current view.
  useEffect(() => {
    if (!view) return;
    let live = true;
    setBusy(true);
    api
      .flow(view)
      .then((g) => {
        if (!live) return;
        setGraph(g);
        setError(null);
        // A diagram that is just START -> one step -> END says nothing yet:
        // open that step straight away (a few levels at most).
        if (view.at) return;
        if (view.expanded.length === 0) autoOpened.current = 0;
        const lone = loneStep(g, view.expanded[view.expanded.length - 1] ?? null);
        if (lone && autoOpened.current < 3 && view.expanded.length === autoOpened.current) {
          autoOpened.current += 1;
          setOpening((n) => n + 1);
          go({ ...view, expanded: [...view.expanded, lone.id] }, "replace");
        }
      })
      .catch((e) => live && setError(String(e)))
      .finally(() => live && setBusy(false));
    return () => {
      live = false;
    };
  }, [view, go]);

  // Lay it out (again when the direction changes).
  useEffect(() => {
    if (!graph) return;
    let live = true;
    layoutGraph(graph, direction)
      .then((p) => live && setPlaced(p))
      .catch((e) => live && setError(String(e)));
    return () => {
      live = false;
    };
  }, [graph, direction]);

  const keys = useRef<(event: KeyboardEvent) => void>(() => undefined);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => keys.current(event);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const flash = (message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(null), 2200);
  };

  const toggle = useCallback(
    (node: FlowNode) => {
      if (!view) return;
      const open = new Set(view.expanded);
      if (node.kind === "group") {
        for (const id of [...open]) if (id === node.id || id.startsWith(`${node.id}/`)) open.delete(id);
      } else {
        open.add(node.id);
      }
      go({ ...view, expanded: [...open] }, "replace", node.id);
    },
    [view, go],
  );

  const openEntry = (entry: Entry) => {
    setSelected(null);
    go({ root: entry.target, start: entry.label, expanded: [] }, "push");
  };

  const enter = (target: string) => {
    setSelected(null);
    go({ root: target, expanded: [] }, "push");
  };

  const follow = (at: string, variable: string) => {
    if (!view) return;
    setSelected(null);
    go({ root: view.root, start: view.start, expanded: view.expanded, at, var: variable }, "push");
  };

  const stopFollowing = () => {
    if (!view) return;
    setSelected(null);
    go({ root: view.root, start: view.start, expanded: view.expanded }, "push");
  };

  const showTrailAs = (trailView: "journey" | "steps") => {
    if (!view?.at) return;
    setSelected(null);
    go({ ...view, trailView: trailView === "journey" ? undefined : trailView }, "replace");
  };

  const openAll = () => {
    if (!graph || !view) return;
    const more = graph.nodes.filter((n) => n.kind === "step" && n.detail.expandable).map((n) => n.id);
    if (more.length) go({ ...view, expanded: [...new Set([...view.expanded, ...more])] }, "replace");
  };

  const copyMermaid = async () => {
    if (!view) return;
    try {
      await navigator.clipboard.writeText(await api.mermaid(view, direction === "RIGHT" ? "LR" : "TD"));
      flash("Mermaid copied to the clipboard");
    } catch (e) {
      flash(`Could not copy: ${e}`);
    }
  };

  // ── simulation ─────────────────────────────────────────────────────────

  const startSimulation = async () => {
    if (!view || view.at) return;
    setSimBusy(true);
    try {
      const data = await api.simulate(view, {});
      setSim((old) => ({ run: (old?.run ?? 0) + 1, data, index: 0, playing: true, choices: {} }));
      setTab("sim");
    } catch (e) {
      flash(`Could not simulate: ${e}`);
    } finally {
      setSimBusy(false);
    }
  };

  const stopSimulation = () => {
    window.clearTimeout(dwell.current);
    setSim(null);
    if (!selected) setTab("details");
  };

  const choose = async (value: string) => {
    if (!view || !sim?.data.choice) return;
    const choices = { ...sim.choices, [sim.data.choice.node]: value };
    setSimBusy(true);
    try {
      const data = await api.simulate(view, choices);
      setSim({ ...sim, data, choices, index: Math.min(sim.index + 1, data.frames.length - 1), playing: true });
    } catch (e) {
      flash(`Could not simulate: ${e}`);
    } finally {
      setSimBusy(false);
    }
  };

  const onArrive = useCallback(() => {
    window.clearTimeout(dwell.current);
    setSim((current) => {
      if (!current?.playing) return current;
      const frame = current.data.frames[current.index];
      const changed = frame.changes.added.length + frame.changes.changed.length > 0;
      dwell.current = window.setTimeout(
        () =>
          setSim((s) => {
            if (!s?.playing) return s;
            if (s.index >= s.data.frames.length - 1) return { ...s, playing: false };
            return { ...s, index: s.index + 1 };
          }),
        (changed ? 1100 : 450) / speed,
      );
      return current;
    });
  }, [speed]);

  const step = (delta: number) => {
    window.clearTimeout(dwell.current);
    setSim((s) => (s ? { ...s, playing: false, index: Math.max(0, Math.min(s.data.frames.length - 1, s.index + delta)) } : s));
  };

  keys.current = (event: KeyboardEvent) => {
    const tag = (event.target as HTMLElement).tagName;
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" || tag === "BUTTON") return;
    if (event.key === "f") setFitSignal((n) => n + 1);
    if (event.key === "Escape") setSelected(null);
    if (!sim) return;
    if (event.key === " ") {
      event.preventDefault();
      if (sim.playing) {
        window.clearTimeout(dwell.current);
        setSim({ ...sim, playing: false });
      } else if (sim.index < sim.data.frames.length - 1) {
        setSim({ ...sim, playing: true, run: sim.run + 1 });
      }
    }
    if (event.key === "ArrowRight") step(1);
    if (event.key === "ArrowLeft") step(-1);
  };

  const tokenRun: TokenRun | null = (() => {
    if (!sim) return null;
    const frame = sim.data.frames[sim.index];
    if (!frame) return null;
    const walked = new Set<string>();
    for (const f of sim.data.frames.slice(0, sim.index + 1)) if (f.edge && !f.hop) walked.add(`${f.edge[0]}>${f.edge[1]}`);
    return {
      key: `${sim.run}:${sim.index}`,
      first: sim.index === 0,
      frame,
      previous: sim.index > 0 ? sim.data.frames[sim.index - 1] : null,
      speed,
      badge: badgeFor(frame),
      walked,
      onArrive,
    };
  })();

  const selectedNode = graph?.nodes.find((n) => n.id === selected) ?? null;
  const title = view?.start ?? graph?.nodes.find((n) => n.kind === "start")?.label ?? "";
  const rightOpen = selectedNode !== null || sim !== null;
  const showing = sim && (tab === "sim" || !selectedNode) ? "sim" : "details";
  const columns = [`${leftWidth}px`, "5px", "minmax(0, 1fr)", ...(rightOpen ? ["5px", `${rightWidth}px`] : [])].join(" ");

  return (
    <div className="app" style={{ gridTemplateColumns: columns }}>
      {project ? (
        <Sidebar project={project} current={view?.root ?? null} onOpen={openEntry} onFunction={enter} />
      ) : (
        <aside className="sidebar" />
      )}
      <Resizer
        label="Resize the entry list"
        onDrag={(dx) => setLeftWidth((w) => clamp(w + dx, LEFT.min, LEFT.max))}
        onReset={() => setLeftWidth(LEFT.initial)}
      />
      <main className="stage">
        <div className="toolbar">
          <button onClick={() => history.back()} title="Back to the previous diagram">
            ←
          </button>
          <div className="crumb">
            <code>{title}</code>
            {view && !view.start && <span className="crumb-file">{view.root.split("::")[0]}</span>}
          </div>
          <div className="spacer" />
          {view?.at && view.var ? (
            <div className="following">
              Following <code>{view.var.startsWith("$") ? "a call's result" : view.var}</code>
              {graph?.trail?.truncated && <span className="following-note"> (cut short: very long trail)</span>}
              <div className="segmented" role="group" aria-label="Show the data as">
                <button
                  className={view.trailView !== "steps" ? "is-on" : ""}
                  onClick={() => showTrailAs("journey")}
                  title="Every function the data passes through, grouped by file"
                >
                  Journey
                </button>
                <button
                  className={view.trailView === "steps" ? "is-on" : ""}
                  onClick={() => showTrailAs("steps")}
                  title="Every step that touches the data"
                >
                  Steps
                </button>
              </div>
              <button onClick={stopFollowing}>Stop following</button>
            </div>
          ) : (
            <>
              <button onClick={openAll} title="Open every step one level deeper">
                Open all
              </button>
              <button onClick={() => view && go({ ...view, expanded: [] }, "replace")} disabled={!view?.expanded.length}>
                Close all
              </button>
            </>
          )}
          <button onClick={() => setTips((t) => !t)} title="How to use this view" aria-label="Help">
            ?
          </button>
          <button
            onClick={() => setDirection((d) => (d === "RIGHT" ? "DOWN" : "RIGHT"))}
            title={direction === "RIGHT" ? "Lay the flow out top to bottom" : "Lay the flow out left to right"}
            aria-label="Switch layout direction"
          >
            {direction === "RIGHT" ? "⇄" : "⇅"}
          </button>
          <button onClick={copyMermaid} title="Copy this diagram as Mermaid text">
            Copy Mermaid
          </button>
          {!view?.at &&
            (sim ? (
              <button className="primary" onClick={stopSimulation} title="Stop the simulation">
                ■ Stop
              </button>
            ) : (
              <button
                className="primary"
                onClick={startSimulation}
                disabled={simBusy || !placed}
                title="Send data through this diagram, step by step"
              >
                ▶ Simulate
              </button>
            ))}
        </div>
        {tips && (
          <div className="tips" role="note">
            <span>
              <b>+</b> opens a step in place
            </span>
            <span>
              click a <u>data label</u> to follow that value through the code
            </span>
            <span>
              <b>▶ Simulate</b> sends data through the diagram (space pauses, ← → step)
            </span>
            <span>drag the panel edges to resize · F fits · ⇄ turns the layout</span>
            <button
              onClick={() => {
                setTips(false);
                localStorage.setItem("flowmap.tips", "off");
              }}
            >
              Got it
            </button>
          </div>
        )}
        {placed && view && (
          <Diagram
            placed={placed}
            rootKey={`${view.root}|${opening}|${view.at ?? ""}|${view.var ?? ""}|${view.trailView ?? ""}|${direction}`}
            anchor={anchor}
            fitSignal={fitSignal}
            selected={selected}
            onSelect={(id) => {
              setSelected(id);
              if (id) setTab("details");
            }}
            onToggle={toggle}
            direction={direction}
            onFollow={view.at && view.trailView !== "steps" ? undefined : follow}
            following={Boolean(view.at)}
            run={tokenRun}
          />
        )}
        {!placed && !error && (
          <div className="empty">{project && !project.entries.length ? "No entry points found in this folder." : "Reading the project…"}</div>
        )}
        {error && <div className="error-banner">{error}</div>}
        {busy && placed && <div className="busy">Laying out…</div>}
        <Legend />
        {toast && <div className="toast">{toast}</div>}
      </main>
      {rightOpen && (
        <Resizer
          label="Resize the side panel"
          onDrag={(dx) => setRightWidth((w) => clamp(w - dx, RIGHT.min, RIGHT.max))}
          onReset={() => setRightWidth(RIGHT.initial)}
        />
      )}
      {rightOpen && (
        <aside className="inspector">
          {sim && (
            <div className="tabs" role="tablist">
              <button role="tab" aria-selected={showing === "sim"} className={showing === "sim" ? "is-on" : ""} onClick={() => setTab("sim")}>
                Simulation
              </button>
              <button
                role="tab"
                aria-selected={showing === "details"}
                className={showing === "details" ? "is-on" : ""}
                onClick={() => setTab("details")}
                disabled={!selectedNode}
                title={selectedNode ? "" : "Click a box in the diagram"}
              >
                Details
              </button>
              <span className="spacer" />
              <button className="close" onClick={stopSimulation} aria-label="Stop the simulation" title="Stop the simulation">
                ×
              </button>
            </div>
          )}
          {showing === "sim" && sim ? (
            <SimPanel
              sim={sim.data}
              index={sim.index}
              playing={sim.playing}
              speed={speed}
              busy={simBusy}
              onPlay={() => setSim((s) => (s ? { ...s, playing: true, run: s.run + 1 } : s))}
              onPause={() => {
                window.clearTimeout(dwell.current);
                setSim((s) => (s ? { ...s, playing: false } : s));
              }}
              onStep={step}
              onRestart={() => {
                window.clearTimeout(dwell.current);
                setSim((s) => (s ? { ...s, index: 0, playing: true, run: s.run + 1 } : s));
              }}
              onSpeed={setSpeed}
              onChoose={choose}
            />
          ) : (
            selectedNode && (
              <Details
                node={selectedNode}
                following={Boolean(view?.at)}
                onToggle={toggle}
                onEnter={enter}
                onFollow={follow}
                onClose={() => setSelected(null)}
              />
            )
          )}
        </aside>
      )}
    </div>
  );
}

/** The only thing inside ``parent`` (or at the top level) when it is a single step that can be opened. */
function loneStep(graph: FlowGraph, parent: string | null): FlowNode | null {
  const inside = graph.nodes.filter((n) => n.parent === parent && n.kind !== "start" && n.kind !== "end");
  return inside.length === 1 && inside[0].kind === "step" && inside[0].detail.expandable ? inside[0] : null;
}

function Legend() {
  return (
    <div className="legend" aria-label="Legend">
      <span className="legend-item">
        <i className="swatch swatch-terminal" /> start / end
      </span>
      <span className="legend-item">
        <i className="swatch swatch-step" /> step (+ opens it)
      </span>
      <span className="legend-item">
        <i className="swatch swatch-io" /> data in / out
      </span>
      <span className="legend-item">
        <i className="swatch swatch-use" /> uses the data
      </span>
      <span className="legend-item">
        <i className="swatch swatch-decision" /> decision
      </span>
      <span className="legend-item">
        <i className="swatch swatch-loop" /> loop
      </span>
      <span className="legend-item">
        <i className="swatch swatch-error" /> error path
      </span>
    </div>
  );
}
