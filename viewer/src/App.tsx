import { useCallback, useEffect, useRef, useState } from "react";
import { api, readHash, writeHash, type ViewState } from "./api";
import { Details } from "./Details";
import { Diagram } from "./Diagram";
import { layoutGraph } from "./elk";
import type { Placed } from "./layout";
import { Sidebar } from "./Sidebar";
import type { Entry, FlowGraph, FlowNode, ProjectInfo } from "./types";

const DETAILS_WIDTH = 380;

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
  const autoOpened = useRef(0);
  // Bumped by automatic opens so the diagram re-frames as for a new root.
  const [opening, setOpening] = useState(0);

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

  useEffect(() => {
    if (!view) return;
    let live = true;
    setBusy(true);
    api
      .flow(view)
      .then((g) =>
        layoutGraph(g).then((p) => {
          if (!live) return;
          setGraph(g);
          setPlaced(p);
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
        }),
      )
      .catch((e) => live && setError(String(e)))
      .finally(() => live && setBusy(false));
    return () => {
      live = false;
    };
  }, [view]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.target as HTMLElement).tagName === "INPUT") return;
      if (event.key === "f") setFitSignal((n) => n + 1);
      if (event.key === "Escape") setSelected(null);
    };
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

  const showTrailAs = (trailView: "journey" | "steps") => {
    if (!view?.at) return;
    setSelected(null);
    go({ ...view, trailView: trailView === "journey" ? undefined : trailView }, "replace");
  };

  const stopFollowing = () => {
    if (!view) return;
    setSelected(null);
    go({ root: view.root, start: view.start, expanded: view.expanded }, "push");
  };

  const openAll = () => {
    if (!graph || !view) return;
    const more = graph.nodes.filter((n) => n.kind === "step" && n.detail.expandable).map((n) => n.id);
    if (more.length) go({ ...view, expanded: [...new Set([...view.expanded, ...more])] }, "replace");
  };

  const copyMermaid = async () => {
    if (!view) return;
    try {
      await navigator.clipboard.writeText(await api.mermaid(view));
      flash("Mermaid copied to the clipboard");
    } catch (e) {
      flash(`Could not copy: ${e}`);
    }
  };

  const selectedNode = graph?.nodes.find((n) => n.id === selected) ?? null;
  const title = view?.start ?? graph?.nodes.find((n) => n.kind === "start")?.label ?? "";

  return (
    <div className="app">
      {project ? <Sidebar project={project} current={view?.root ?? null} onOpen={openEntry} /> : <aside className="sidebar" />}
      <main className={selectedNode ? "stage has-details" : "stage"}>
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
          <button onClick={copyMermaid} title="Copy this diagram as Mermaid text">
            Copy Mermaid
          </button>
        </div>
        {placed && view && (
          <Diagram
            placed={placed}
            rootKey={`${view.root}|${opening}|${view.at ?? ""}|${view.var ?? ""}|${view.trailView ?? ""}`}
            anchor={anchor}
            fitSignal={fitSignal}
            selected={selected}
            onSelect={setSelected}
            onToggle={toggle}
            rightInset={selectedNode ? DETAILS_WIDTH : 0}
            onFollow={view.at && view.trailView !== "steps" ? undefined : follow}
            following={Boolean(view.at)}
          />
        )}
        {!placed && !error && <div className="empty">{project && !project.entries.length ? "No entry points found in this folder." : "Reading the project…"}</div>}
        {error && <div className="error-banner">{error}</div>}
        {busy && placed && <div className="busy">Laying out…</div>}
        <Legend />
        {toast && <div className="toast">{toast}</div>}
        {selectedNode && (
          <Details
            node={selectedNode}
            following={Boolean(view?.at)}
            onToggle={toggle}
            onEnter={enter}
            onFollow={follow}
            onClose={() => setSelected(null)}
          />
        )}
      </main>
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
