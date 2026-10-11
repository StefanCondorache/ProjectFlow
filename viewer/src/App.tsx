import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { api, readHash, toggleFolderExpansion, writeHash, type ViewState } from "./api";
import { Details } from "./Details";
import { Diagram, type TokenRun } from "./Diagram";
import { Icon } from "./Icon";
import { MapDetails } from "./MapDetails";
import { layoutGraph } from "./elk";
import type { Direction, Placed } from "./layout";
import { Resizer } from "./Resizer";
import { RunSetup } from "./RunSetup";
import { Sidebar } from "./Sidebar";
import { badgeFor, NO_SETUP, type Expects, type SimSetup, type Simulation } from "./sim";
import { SimPanel } from "./SimPanel";
import type { Entry, FlowGraph, FlowNode, ProjectInfo } from "./types";

interface SimState {
  run: number; // bumps on every (re)start, so the token restarts
  data: Simulation;
  index: number;
  playing: boolean;
  choices: Record<string, string>;
  setup: SimSetup;
}

/** The form for the data a run starts with, while it is open. */
interface SetupState {
  setup: SimSetup;
  label: string;
  expects: Expects | null;
}

const sameSet = (a: string[], b: string[]) => a.length === b.length && [...a].sort().join(",") === [...b].sort().join(",");

const LEFT = { initial: 292, min: 240, max: 480 };
const RIGHT = { initial: 380, min: 320, max: 640 };
const clamp = (value: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, value));

function stored<T extends string | number>(key: string, fallback: T): T {
  const value = localStorage.getItem(`flowmap.${key}`);
  if (value === null) return fallback;
  return (typeof fallback === "number" ? Number(value) : value) as T;
}

export function App() {
  const [project, setProject] = useState<ProjectInfo | null>(null);
  const [view, setView] = useState<ViewState | null>(null);
  const map = !view || view.mode === "map";
  const supportsSimulation = Boolean(view && !map && !view.at && view.root.split("::")[0].endsWith(".py") && (project?.capabilities?.simulation_languages ?? ["python"]).includes("python"));
  const currentView = useRef(view);
  currentView.current = view;
  const [graph, setGraph] = useState<FlowGraph | null>(null);
  const [graphView, setGraphView] = useState<{ view: ViewState; key: string } | null>(null);
  const [rendered, setRendered] = useState<{ placed: Placed; view: ViewState; key: string; direction: Direction } | null>(null);
  const placed = rendered?.placed ?? null;
  const [layingOut, setLayingOut] = useState(false);
  const [retry, setRetry] = useState(0);
  const [desktop, setDesktop] = useState(() => window.matchMedia("(min-width: 761px)").matches);
  const [sidebarOpen, setSidebarOpen] = useState(() => window.matchMedia("(min-width: 761px)").matches);
  const [selected, setSelected] = useState<string | null>(null);
  const [anchor, setAnchor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [fitSignal, setFitSignal] = useState(0);
  const [toast, setToast] = useState<string | null>(null);
  const [direction, setDirection] = useState<Direction>(() => stored<Direction>("direction", "RIGHT"));
  const [leftWidth, setLeftWidth] = useState(() => clamp(stored("left", LEFT.initial) || LEFT.initial, LEFT.min, LEFT.max));
  const [rightWidth, setRightWidth] = useState(() => clamp(stored("right", RIGHT.initial) || RIGHT.initial, RIGHT.min, RIGHT.max));
  const autoOpened = useRef(0);
  // Bumped by automatic opens so the diagram re-frames as for a new root.
  const [opening, setOpening] = useState(0);
  const [sim, setSim] = useState<SimState | null>(null);
  const [setupState, setSetupState] = useState<SetupState | null>(null);
  const [simBusy, setSimBusy] = useState(false);
  const [speed, setSpeed] = useState<number>(() => stored<number>("speed", 1));
  const [tab, setTab] = useState<"sim" | "details">("details");
  const [tips, setTips] = useState(() => stored("tips", "on") === "on");
  const dwell = useRef<number | undefined>(undefined);
  const simulationRequest = useRef(0);
  const setupRequest = useRef(0);
  const navigationToggle = useRef<HTMLButtonElement>(null);

  useEffect(() => localStorage.setItem("flowmap.direction", direction), [direction]);
  useEffect(() => localStorage.setItem("flowmap.left", String(leftWidth)), [leftWidth]);
  useEffect(() => localStorage.setItem("flowmap.right", String(rightWidth)), [rightWidth]);
  useEffect(() => localStorage.setItem("flowmap.speed", String(speed)), [speed]);

  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 761px)");
    const onChange = (event: MediaQueryListEvent) => {
      setDesktop(event.matches);
      setSidebarOpen(event.matches);
    };
    desktop.addEventListener("change", onChange);
    return () => desktop.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    if (desktop || !sidebarOpen) return;
    const drawer = document.querySelector<HTMLElement>(".sidebar");
    if (!drawer) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    const controls = () => [...drawer.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), [tabindex='0']")].filter((element) => element.getClientRects().length > 0);
    const first = drawer.querySelector<HTMLElement>(".navigation-close") ?? controls()[0];
    const focusFrame = window.requestAnimationFrame(() => first?.focus({ preventScroll: true }));
    const trapFocus = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const items = controls();
      const next = event.shiftKey ? items[items.length - 1] : items[0];
      const edge = event.shiftKey ? items[0] : items[items.length - 1];
      if (document.activeElement === edge || !drawer.contains(document.activeElement)) {
        event.preventDefault();
        next?.focus({ preventScroll: true });
      }
    };
    document.addEventListener("keydown", trapFocus);
    return () => {
      document.removeEventListener("keydown", trapFocus);
      window.cancelAnimationFrame(focusFrame);
      const opener = previousFocus?.isConnected && previousFocus !== document.body ? previousFocus : navigationToggle.current;
      opener?.focus({ preventScroll: true });
    };
  }, [desktop, sidebarOpen, project]);

  // A simulation belongs to the function it was started on; opening or
  // closing steps in it walks it again (see below).
  const simKey = view ? JSON.stringify([view.mode ?? "flow", view.root, view.path ?? "", view.start ?? "", view.at ?? "", view.var ?? ""]) : "";
  const currentSimKey = useRef(simKey);
  currentSimKey.current = simKey;
  useEffect(() => {
    simulationRequest.current += 1;
    setupRequest.current += 1;
    window.clearTimeout(dwell.current);
    setSim(null);
    setSetupState(null);
    setSimBusy(false);
  }, [simKey]);

  const go = useCallback((next: ViewState, mode: "push" | "replace", anchorId: string | null = null) => {
    const before = currentView.current;
    if (next.mode !== before?.mode || next.root !== before?.root || next.path !== before?.path) setSelected(null);
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
        else go({ mode: "map", root: "@project", path: "", expanded: [] }, "replace");
      })
      .catch((e) => setError(String(e)));
    const onPop = () => {
      const state = readHash() ?? { mode: "map" as const, root: "@project", path: "", expanded: [] };
      setAnchor(null);
      setSelected(null);
      setView(state);
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
        setGraphView({ view, key: JSON.stringify([view.mode ?? "flow", view.root, view.path ?? "", opening, view.at ?? "", view.var ?? "", view.trailView ?? ""]) });
        setError(null);
        // A diagram that is just START -> one step -> END says nothing yet:
        // open that step straight away (a few levels at most).
        if (view.mode === "map" || view.at) return;
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
  }, [view, go, retry]);

  // Lay it out (again when the direction changes).
  useEffect(() => {
    if (!graph || !graphView) return;
    let live = true;
    setLayingOut(true);
    layoutGraph(graph, direction)
      .then((p) => {
        if (!live) return;
        setRendered({ placed: p, view: graphView.view, key: `${graphView.key}|${direction}`, direction });
      })
      .catch((e) => live && setError(String(e)))
      .finally(() => live && setLayingOut(false));
    return () => {
      live = false;
    };
  }, [graph, graphView, direction]);

  useEffect(() => {
    if (project) document.title = `${project.name} · Flowmap`;
  }, [project]);

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
      if (view.mode === "map") {
        if (node.kind === "folder" && node.detail.expandable !== false) go({ ...view, expanded: toggleFolderExpansion(view.expanded, node.id) }, "replace", node.id);
        return;
      }
      const open = new Set(view.expanded);
      if (node.kind === "group") {
        for (const id of [...open]) if (id === node.id || id.startsWith(`${node.id}/`)) open.delete(id);
        // Closing a box by hand takes over from "go inside every step".
        setSim((s) => (s?.setup.autoOpen ? { ...s, setup: { ...s.setup, autoOpen: false } } : s));
      } else {
        open.add(node.id);
      }
      go({ ...view, expanded: [...open] }, "replace", node.id);
    },
    [view, go],
  );

  const openEntry = (entry: Entry) => {
    setSelected(null);
    if (window.matchMedia("(max-width: 760px)").matches) setSidebarOpen(false);
    go({ root: entry.target, start: entry.label, expanded: [] }, "push");
  };

  const enter = (target: string) => {
    setSelected(null);
    if (window.matchMedia("(max-width: 760px)").matches) setSidebarOpen(false);
    go({ root: target, expanded: [] }, "push");
  };

  const focusMap = (path: string) => {
    setSelected(null);
    if (window.matchMedia("(max-width: 760px)").matches) setSidebarOpen(false);
    go({ mode: "map", root: "@project", path, expanded: [] }, "push");
  };

  const follow = (at: string, variable: string) => {
    if (!view || view.mode === "map") return;
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
    const more = graph.nodes.filter((n) => n.kind === (map ? "folder" : "step") && n.detail.expandable).map((n) => n.id);
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

  /** Show the steps a run went into (with "go inside every step" it opens more). */
  const syncExpanded = (data: Simulation) => {
    if (view && !sameSet(data.expanded, view.expanded)) go({ ...view, expanded: data.expanded }, "replace");
  };

  const openSetup = (at: string | null, label: string) => {
    if (!view || !supportsSimulation) return;
    const base = sim?.setup ?? NO_SETUP;
    const setup = { ...base, at, inputs: at === base.at ? base.inputs : {} };
    const request = ++setupRequest.current;
    setSetupState({ setup, label, expects: null });
    setTab("sim");
    api
      .expects(view, at ?? "s")
      .then((expects) => {
        if (request === setupRequest.current && simKey === currentSimKey.current) setSetupState((s) => (s ? { ...s, expects } : s));
      })
      .catch((e) => {
        if (request === setupRequest.current && simKey === currentSimKey.current) flash(`Could not look at the data there: ${e}`);
      });
  };

  const runSimulation = async (setup: SimSetup) => {
    if (!view || !supportsSimulation) return;
    const request = ++simulationRequest.current;
    const active = () => request === simulationRequest.current && simKey === currentSimKey.current;
    setSimBusy(true);
    try {
      const data = await api.simulate(view, {}, setup);
      if (!active()) return;
      setSim((old) => ({ run: (old?.run ?? 0) + 1, data, index: 0, playing: true, choices: {}, setup }));
      setSetupState(null);
      setTab("sim");
      syncExpanded(data);
    } catch (e) {
      if (active()) flash(`Could not simulate: ${e}`);
    } finally {
      if (active()) setSimBusy(false);
    }
  };

  /** Walk again with other data or answers, staying at the same frame. */
  const rerun = async (setup: SimSetup, choices: Record<string, string>, index: number, advance = false) => {
    if (!view || !supportsSimulation) return;
    const request = ++simulationRequest.current;
    const active = () => request === simulationRequest.current && simKey === currentSimKey.current;
    setSimBusy(true);
    try {
      const data = await api.simulate(view, choices, setup);
      if (!active()) return;
      const at = Math.max(0, Math.min(index + (advance ? 1 : 0), data.frames.length - 1));
      setSim((s) => (s ? { ...s, data, choices, setup, index: at, playing: advance || s.playing } : s));
      syncExpanded(data);
    } catch (e) {
      if (active()) flash(`Could not simulate: ${e}`);
    } finally {
      if (active()) setSimBusy(false);
    }
  };

  // Opening or closing a step while a run is on: walk the new diagram.
  const expandedKey = view ? [...view.expanded].sort().join(",") : "";
  useEffect(() => {
    if (!sim || !view || view.mode === "map" || view.at || sameSet(view.expanded, sim.data.expanded)) return;
    void rerun(sim.setup, sim.choices, sim.index);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expandedKey]);

  const stopSimulation = () => {
    simulationRequest.current += 1;
    setupRequest.current += 1;
    window.clearTimeout(dwell.current);
    setSim(null);
    setSetupState(null);
    setSimBusy(false);
    if (!selected) setTab("details");
  };

  const choose = (value: string) => {
    if (!sim?.data.choice) return;
    void rerun(sim.setup, { ...sim.choices, [sim.data.choice.node]: value }, sim.index, true);
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
    if (event.defaultPrevented) return;
    if (event.key === "Escape") {
      setSelected(null);
      if (window.matchMedia("(max-width: 760px)").matches) setSidebarOpen(false);
    }
    const tag = (event.target as HTMLElement).tagName;
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" || tag === "BUTTON") return;
    if (event.key === "f") setFitSignal((n) => n + 1);
    if (!sim || map) return;
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
    if (!sim || map) return null;
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
  const title = map ? (view?.path ? view.path.split("/").pop()! : "Project map") : view?.start ?? (graphView?.view === view ? graph?.nodes.find((n) => n.kind === "start")?.label : view?.root.split("::").slice(1).join("::")) ?? "";
  const simulating = !map && (sim !== null || setupState !== null);
  const rightOpen = selectedNode !== null || simulating;
  const showing = simulating && (tab === "sim" || !selectedNode) ? "sim" : "details";
  const frameNode = sim ? (graph?.nodes.find((n) => n.id === sim.data.frames[sim.index]?.node) ?? null) : null;
  const diagramReady = Boolean(rendered && rendered.view === view && rendered.direction === direction);
  const updating = busy || layingOut || Boolean(view && !diagramReady && !error);
  const emptyMap = Boolean(map && diagramReady && graph?.nodes.length === 0);
  const pathParts = (view?.path ?? "").split("/").filter(Boolean);
  const overview = graphView?.view === view ? graph?.overview : undefined;
  const summary = graph && graphView?.view === view ? {
    steps: graph.nodes.filter((n) => ["step", "io", "use", "raise"].includes(n.kind)).length,
    decisions: graph.nodes.filter((n) => n.kind === "decision").length,
    opened: graph.nodes.filter((n) => n.kind === "group").length,
  } : null;

  return (
    <div className={`app${sidebarOpen ? " is-sidebar-open" : ""}${rightOpen ? " has-inspector" : ""}`} style={{ "--left-width": `${leftWidth}px`, "--right-width": `${rightWidth}px` } as CSSProperties}>
      <button className="navigation-backdrop" tabIndex={-1} aria-label="Close project navigation" onClick={() => setSidebarOpen(false)} />
      {project ? (
        <Sidebar project={project} current={view?.root ?? null} onOpen={openEntry} onFunction={enter} map={map} path={view?.path} onMap={() => focusMap("")} onFile={focusMap} modal={!desktop && sidebarOpen} onClose={() => setSidebarOpen(false)} />
      ) : (
        <aside className="sidebar sidebar-loading" aria-label="Loading project"><div className="brand">flowmap</div><div className="skeleton" /><div className="skeleton" /><div className="skeleton" /></aside>
      )}
      <Resizer
        className="left-resizer"
        label="Resize the entry list"
        onDrag={(dx) => setLeftWidth((w) => clamp(w + dx, LEFT.min, LEFT.max))}
        onReset={() => setLeftWidth(LEFT.initial)}
      />
      <main className="stage" inert={!desktop && sidebarOpen}>
        <header className="workspace-header">
          <div className="stage-heading">
            <button ref={navigationToggle} className="icon-button navigation-toggle" onClick={() => setSidebarOpen((open) => !open)} aria-label="Toggle project navigation" aria-expanded={sidebarOpen} title="Toggle project navigation"><Icon name="panels" size={19} /></button>
            <div className="heading-copy"><div className="eyebrow">{map ? "Project explorer" : view?.at ? "Data explorer" : "Flow explorer"}</div><h1 title={title || "Flowmap"}>{title || project?.name || "Your code, mapped out"}</h1></div>
            <span className={`view-status${updating || !project ? " is-busy" : ""}${error ? " is-error" : ""}`} role="status"><span className="status-dot" />{error ? "Needs attention" : !project ? "Reading" : updating ? "Updating" : "Ready"}</span>
          </div>
          <div className="flow-context">
            {map ? <nav className="map-breadcrumbs" aria-label="Map location">
              <button onClick={() => focusMap("")} aria-current={pathParts.length === 0 ? "page" : undefined}>{project?.name ?? "Project"}</button>
              {pathParts.map((part, i) => <span key={pathParts.slice(0, i + 1).join("/")}><span aria-hidden="true"> / </span><button onClick={() => focusMap(pathParts.slice(0, i + 1).join("/"))} aria-current={i === pathParts.length - 1 ? "page" : undefined}>{part}</button></span>)}
            </nav> : <span className="crumb-file" title={view?.root.split("::")[0]}><Icon name="code" size={14} />{view?.root.split("::")[0] || "Reading your project"}</span>}
            {map && overview ? <div className="flow-summary" aria-label="Map summary"><span><strong>{overview.files}</strong> files</span><span><strong>{overview.relationships}</strong> imports</span><span><strong>{overview.parsed_files}</strong> parsed</span></div> : !map && summary && <div className="flow-summary" aria-label="Diagram summary"><span><strong>{summary.steps}</strong> steps</span><span><strong>{summary.decisions}</strong> decisions</span><span><strong>{summary.opened}</strong> opened</span></div>}
          </div>
        </header>
        <div className="toolbar" aria-label="Diagram controls">
          <button className="icon-button" onClick={() => history.back()} title="Back to the previous diagram" aria-label="Back to the previous diagram"><Icon name="back" /></button>
          {(!map || view?.path) && <button onClick={() => focusMap("")} title="Return to the full project map"><Icon name="panels" />{map ? "Back to project" : "Project map"}</button>}
          {!map && view?.at && view.var ? (
            <div className="following">
              Following <code>{view.var.startsWith("$") ? "a call's result" : view.var}</code>
              {graph?.trail?.truncated && <span className="following-note"> (cut short: very long trail)</span>}
              <div className="segmented" role="group" aria-label="Show the data as">
                <button
                  className={view.trailView !== "steps" ? "is-on" : ""}
                  aria-pressed={view.trailView !== "steps"}
                  onClick={() => showTrailAs("journey")}
                  title="Every function the data passes through, grouped by file"
                >
                  Journey
                </button>
                <button
                  className={view.trailView === "steps" ? "is-on" : ""}
                  aria-pressed={view.trailView === "steps"}
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
              <button onClick={openAll} disabled={!diagramReady || updating || emptyMap} title={map ? "Expand every visible folder one level deeper" : "Open every step one level deeper"}>
                <Icon name="expand" /> <span>{map ? "Expand folders" : "Open all"}</span>
              </button>
              <button onClick={() => view && go({ ...view, expanded: [] }, "replace")} disabled={!view?.expanded.length || !diagramReady || updating}>
                <Icon name="collapse" /> <span>{map ? "Collapse folders" : "Close all"}</span>
              </button>
            </>
          )}
          <div className="spacer" />
          <button className="icon-button" onClick={() => setTips((t) => !t)} title="How to use this view" aria-label="Help" aria-expanded={tips}>
            <Icon name="help" />
          </button>
          <button
            onClick={() => setDirection((d) => (d === "RIGHT" ? "DOWN" : "RIGHT"))}
            title={direction === "RIGHT" ? "Arrange the diagram top to bottom" : "Arrange the diagram left to right"}
            aria-label="Switch layout direction"
            disabled={!placed || updating}
          >
            <Icon name="layout" /> <span>{direction === "RIGHT" ? "Horizontal" : "Vertical"}</span>
          </button>
          <button onClick={copyMermaid} disabled={!diagramReady || updating} title="Copy this diagram as Mermaid text">
            <Icon name="code" /> <span>Copy Mermaid</span>
          </button>
          {supportsSimulation &&
            (sim ? (
              <button className="primary" onClick={stopSimulation} title="Stop the simulation">
                <Icon name="stop" /> Stop
              </button>
            ) : (
              <button
                className="primary"
                onClick={() => openSetup(null, title)}
                disabled={simBusy || !diagramReady || updating}
                title="Send data through this diagram, step by step"
              >
                <Icon name="play" /> Simulate
              </button>
            ))}
        </div>
        {tips && (
          <div className="tips" role="note">
            {map ? <><span><b>+</b> opens a folder in place</span><span>Select a file to see imports, source, and supported function flows</span><span>Arrows show imports; dashed links are inferred</span></> : <>
              <span><b>+</b> opens a step in place</span>
              <span>click a <u>data label</u> to follow that value through the code</span>
              <span><b>▶ Simulate</b> sends data through the diagram (space pauses, ← → step)</span>
            </>}
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
        {map && overview?.truncated && <p className="map-notice" role="status">This map shows a limited number of items. Focus a folder or search for a file to explore a smaller area.</p>}
        {placed && rendered && !emptyMap && (
          <div className={`diagram-container${updating || error ? " is-updating" : ""}`} aria-busy={updating} inert={updating || Boolean(error)}><Diagram
            placed={placed}
            rootKey={rendered.key}
            anchor={anchor}
            fitSignal={fitSignal}
            selected={selected}
            onSelect={(id) => {
              setSelected(id);
              if (id) setTab("details");
            }}
            onToggle={toggle}
            direction={rendered.direction}
            map={rendered.view.mode === "map"}
            onFollow={rendered.view.mode === "map" || (rendered.view.at && rendered.view.trailView !== "steps") ? undefined : follow}
            following={rendered.view.mode !== "map" && Boolean(rendered.view.at)}
            run={tokenRun}
          /></div>
        )}
        {(!placed || emptyMap) && (
          <div className="diagram-container">{!error && <div className="empty" role="status"><div className="empty-icon"><Icon name={map ? "panels" : "flow"} size={30} /></div><h2>{emptyMap ? (view?.path ? "No files in this area" : "No project files found") : "Mapping your project"}</h2><p>{emptyMap ? "This area contains no inventoried files. Generated and ignored folders are excluded from the map." : map ? "Finding the files, folders, and dependencies in your project." : "Finding the steps, decisions, and data that connect your code."}</p>{!emptyMap && <span className="loading-dots" aria-hidden="true"><i /><i /><i /></span>}</div>}</div>
        )}
        {error && <div className="error-banner" role="alert"><strong>Unable to load this view</strong><p>{error}</p><button onClick={() => project ? setRetry((n) => n + 1) : window.location.reload()}>Try again</button></div>}
        {updating && placed && !error && <div className="busy" role="status"><span className="status-dot" />{layingOut ? "Arranging the diagram…" : map ? "Loading the map…" : "Loading the flow…"}</div>}
        <footer className="stage-footer"><Legend map={map} /><span className="canvas-hint">Drag to pan <span>·</span> <kbd>F</kbd> to fit</span></footer>
        {toast && <div className="toast" role="status"><Icon name="check" />{toast}</div>}
      </main>
      {rightOpen && (
        <Resizer
          className="right-resizer"
          label="Resize the side panel"
          onDrag={(dx) => setRightWidth((w) => clamp(w - dx, RIGHT.min, RIGHT.max))}
          onReset={() => setRightWidth(RIGHT.initial)}
        />
      )}
      {rightOpen && (
        <aside className="inspector" aria-label={showing === "sim" ? "Simulation inspector" : map ? "Map inspector" : "Step inspector"} inert={!desktop && sidebarOpen}>
          {simulating && (
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
          {showing === "sim" && setupState ? (
            <RunSetup
              key={setupState.setup.at ?? "start"}
              label={setupState.label}
              expects={setupState.expects}
              setup={setupState.setup}
              busy={simBusy}
              onRun={runSimulation}
              onCancel={() => {
                setupRequest.current += 1;
                simulationRequest.current += 1;
                setSimBusy(false);
                setSetupState(null);
              }}
            />
          ) : showing === "sim" && sim ? (
            <SimPanel
              sim={sim.data}
              index={sim.index}
              node={frameNode}
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
              onSetup={() => {
                const at = sim.setup.at;
                openSetup(at, at ? (graph?.nodes.find((n) => n.id === at)?.label ?? at) : title);
              }}
              onGoInside={toggle}
              onProvide={(nodeId, value) => void rerun({ ...sim.setup, provided: { ...sim.setup.provided, [nodeId]: value } }, sim.choices, sim.index)}
            />
          ) : (
            selectedNode && (
              map && graph ? <MapDetails node={selectedNode} graph={graph} onToggle={toggle} onFocus={focusMap} onEnter={enter} onClose={() => setSelected(null)} /> : <Details
                node={selectedNode}
                following={Boolean(view?.at)}
                onToggle={toggle}
                onEnter={enter}
                onFollow={follow}
                onSimulateFrom={!supportsSimulation ? undefined : (node) => openSetup(node.id === "s" ? null : node.id, node.label)}
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

function Legend({ map }: { map: boolean }) {
  if (map) return <div className="legend" aria-label="Map legend">
    <span className="legend-item"><i className="swatch swatch-folder" /> folder (+ expands)</span>
    <span className="legend-item"><i className="swatch swatch-step" /> project file</span>
    <span className="legend-item"><i className="swatch swatch-dependency" /> verified import</span>
    <span className="legend-item"><i className="swatch swatch-inferred" /> inferred import</span>
  </div>;
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
