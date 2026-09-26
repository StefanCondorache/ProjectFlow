import type { FlowGraph, ProjectInfo, SourceLines } from "./types";

export interface ViewState {
  root: string;
  start?: string;
  expanded: string[];
  /** When set, the diagram follows one piece of data: variable ``var`` from node ``at``. */
  at?: string;
  var?: string;
  /** How a followed piece of data is shown: functions by file, or every step. */
  trailView?: "journey" | "steps";
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}: ${path}`);
  return response.json() as Promise<T>;
}

function flowQuery(view: ViewState): string {
  const params = new URLSearchParams({ root: view.root });
  if (view.start) params.set("start", view.start);
  if (view.at && view.var) {
    params.set("at", view.at);
    params.set("var", view.var);
    params.set("view", view.trailView ?? "journey");
  } else {
    params.set("expand", view.expanded.join(","));
  }
  return params.toString();
}

export const api = {
  project: () => get<ProjectInfo>("api/project"),
  flow: (view: ViewState) => get<FlowGraph>(`api/${view.at ? "trail" : "flow"}?${flowQuery(view)}`),
  source: (file: string, start: number, end: number) =>
    get<SourceLines>(`api/source?${new URLSearchParams({ file, start: String(start), end: String(end) })}`),
  mermaid: async (view: ViewState) => {
    const response = await fetch(`api/mermaid?${flowQuery(view)}`);
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    return response.text();
  },
};

// The current view lives in the URL hash, so views can be bookmarked and the
// browser's back button walks back through the functions you opened.
export function readHash(): ViewState | null {
  const params = new URLSearchParams(location.hash.slice(1));
  const root = params.get("root");
  if (!root) return null;
  return {
    root,
    start: params.get("start") ?? undefined,
    expanded: (params.get("expand") ?? "").split(",").filter(Boolean),
    at: params.get("at") ?? undefined,
    var: params.get("var") ?? undefined,
    trailView: params.get("view") === "steps" ? "steps" : undefined,
  };
}

export function writeHash(view: ViewState, mode: "push" | "replace"): void {
  const params = new URLSearchParams({ root: view.root });
  if (view.start) params.set("start", view.start);
  if (view.expanded.length) params.set("expand", view.expanded.join(","));
  if (view.at && view.var) {
    params.set("at", view.at);
    params.set("var", view.var);
    if (view.trailView === "steps") params.set("view", "steps");
  }
  const url = `#${params}`;
  if (url === location.hash) return;
  if (mode === "push") history.pushState(null, "", url);
  else history.replaceState(null, "", url);
}
