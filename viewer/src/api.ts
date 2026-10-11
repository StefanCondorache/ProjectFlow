import type { Expects, SimSetup, Simulation } from "./sim";
import type { FlowGraph, MapSearchResult, ProjectInfo, SourceLines } from "./types";

export interface ViewState {
  root: string;
  /** Project structure is separate from function execution and data trails. */
  mode?: "map";
  path?: string;
  start?: string;
  expanded: string[];
  /** When set, the diagram follows one piece of data: variable ``var`` from node ``at``. */
  at?: string;
  var?: string;
  /** How a followed piece of data is shown: functions by file, or every step. */
  trailView?: "journey" | "steps";
}

export interface SearchHit {
  id: string;
  label: string;
  file: string;
  line: number;
  doc: string | null;
}

export function languageName(language: string): string {
  const names: Record<string, string> = { python: "Python", javascript: "JavaScript", typescript: "TypeScript", markdown: "Markdown", json: "JSON", yaml: "YAML", toml: "TOML", html: "HTML", css: "CSS", shell: "Shell", unknown: "Other files" };
  return names[language] ?? language;
}

/** Folder IDs use project paths; closing an area closes only its path subtree. */
export function toggleFolderExpansion(expanded: string[], id: string): string[] {
  return expanded.includes(id) ? expanded.filter((item) => item !== id && !item.startsWith(`${id}/`)) : [...expanded, id];
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}: ${path}`);
  return response.json() as Promise<T>;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!response.ok) {
    const reason = await response.json().catch(() => null);
    throw new Error(reason?.error ?? `${response.status} ${response.statusText}: ${path}`);
  }
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

function mapQuery(view: ViewState): string {
  const params = new URLSearchParams({ path: view.path ?? "" });
  for (const id of view.expanded) params.append("open", id);
  return params.toString();
}

export const api = {
  project: () => get<ProjectInfo>("api/project"),
  search: (q: string) => get<SearchHit[]>(`api/search?${new URLSearchParams({ q })}`),
  mapSearch: (q: string) => get<MapSearchResult[]>(`api/map-search?${new URLSearchParams({ q })}`),
  flow: (view: ViewState) => get<FlowGraph>(view.mode === "map" ? `api/overview?${mapQuery(view)}` : `api/${view.at ? "trail" : "flow"}?${flowQuery(view)}`),
  source: (file: string, start: number, end: number) =>
    get<SourceLines>(`api/source?${new URLSearchParams({ file, start: String(start), end: String(end) })}`),
  mapSource: (file: string, start: number, end: number) =>
    get<SourceLines>(`api/map-source?${new URLSearchParams({ file, start: String(start), end: String(end) })}`),
  simulate: (view: ViewState, choices: Record<string, string>, setup: SimSetup) =>
    post<Simulation>("api/simulate", {
      root: view.root,
      expand: view.expanded,
      start: view.start ?? null,
      choices,
      inputs: setup.inputs,
      env: setup.env,
      argv: setup.argv,
      provided: setup.provided,
      start_at: setup.at,
      auto_open: setup.autoOpen,
    }),
  expects: (view: ViewState, at: string) =>
    get<Expects>(`api/expects?${new URLSearchParams({ root: view.root, expand: view.expanded.join(","), at })}`),
  mermaid: async (view: ViewState, dir: "LR" | "TD") => {
    const query = view.mode === "map" ? `mode=map&${mapQuery(view)}` : flowQuery(view);
    const response = await fetch(`api/mermaid?${query}&dir=${dir}`);
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    return response.text();
  },
};

// The current view lives in the URL hash, so views can be bookmarked and the
// browser's back button walks back through the functions you opened.
export function readHash(): ViewState | null {
  const params = new URLSearchParams(location.hash.slice(1));
  const expanded = (params.get("expand") ?? "").split(",").filter(Boolean);
  if (params.get("mode") === "map") return { mode: "map", root: "@project", path: params.get("path") ?? "", expanded: params.has("open") ? params.getAll("open").filter(Boolean) : expanded };
  const root = params.get("root");
  if (!root) return null;
  return {
    root,
    start: params.get("start") ?? undefined,
    expanded,
    at: params.get("at") ?? undefined,
    var: params.get("var") ?? undefined,
    trailView: params.get("view") === "steps" ? "steps" : undefined,
  };
}

export function writeHash(view: ViewState, mode: "push" | "replace"): void {
  const params = new URLSearchParams({ root: view.mode === "map" ? "@project" : view.root });
  if (view.mode === "map") {
    params.set("mode", "map");
    if (view.path) params.set("path", view.path);
  } else if (view.start) params.set("start", view.start);
  if (view.mode === "map") {
    for (const id of view.expanded) params.append("open", id);
  } else if (view.expanded.length) params.set("expand", view.expanded.join(","));
  if (view.mode !== "map" && view.at && view.var) {
    params.set("at", view.at);
    params.set("var", view.var);
    if (view.trailView === "steps") params.set("view", "steps");
  }
  const url = `#${params}`;
  if (url === location.hash) return;
  if (mode === "push") history.pushState(null, "", url);
  else history.replaceState(null, "", url);
}
