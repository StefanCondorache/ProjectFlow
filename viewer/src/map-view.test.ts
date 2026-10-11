import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { api, readHash, toggleFolderExpansion, writeHash, type ViewState } from "./api";
import { Sidebar, requestMapSearch } from "./Sidebar";
import { MapDetails, requestMapSource } from "./MapDetails";
import type { FlowGraph, FlowNode, MapSearchResult, ProjectInfo, SourceLines } from "./types";

beforeEach(() => {
  const location = { hash: "" };
  vi.stubGlobal("location", location);
  vi.stubGlobal("history", {
    pushState: (_state: unknown, _title: string, url: string) => { location.hash = url; },
    replaceState: (_state: unknown, _title: string, url: string) => { location.hash = url; },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("map navigation", () => {
  it("collapses a folder and its expanded descendants without closing a similarly named sibling", () => {
    expect(toggleFolderExpansion(["dir:src", "dir:src/util", "dir:src/util/nested", "dir:src-other"], "dir:src")).toEqual(["dir:src-other"]);
    expect(toggleFolderExpansion(["dir:src-other"], "dir:src")).toEqual(["dir:src-other", "dir:src"]);
  });

  it("shows the inventory's languages and preserves unknown entry kinds in navigation", () => {
    const project: ProjectInfo = {
      name: "Mixed project", root: "/project", files: ["main.py"], stats: { files: 1, functions: 1, classes: 0, calls: 0, project_calls: 0 },
      inventory: { files: 8, parsed_files: 1, languages: { typescript: 5, python: 1, markdown: 2 } },
      capabilities: { overview: true, flow_languages: ["python"], simulation_languages: ["python"] },
      entries: [{ target: "main.py::main", label: "Custom entry", kind: "custom-launcher", sources: [], reach: 1 }],
    };
    const html = renderToStaticMarkup(createElement(Sidebar, { project, current: "@project", map: true, onOpen: () => undefined, onFunction: () => undefined, onMap: () => undefined, onFile: () => undefined }));
    expect(html).toContain("Project map");
    expect(html).toContain("TypeScript");
    expect(html).toContain("Markdown");
    expect(html).toContain("Custom entry");
  });

  it("keeps late file search results separate from the active query", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("window", globalThis);
    let resolveOld!: (hits: MapSearchResult[]) => void;
    let resolveNew!: (hits: MapSearchResult[]) => void;
    const old = new Promise<MapSearchResult[]>((resolve) => { resolveOld = resolve; });
    const newest = new Promise<MapSearchResult[]>((resolve) => { resolveNew = resolve; });
    vi.spyOn(api, "mapSearch").mockImplementation((q) => q === "old" ? old : newest);
    const displayed: MapSearchResult[][] = [];
    const cancelOld = requestMapSearch("old", (hits) => displayed.push(hits), () => undefined);
    await vi.advanceTimersByTimeAsync(150);
    cancelOld();
    const cancelNew = requestMapSearch("new", (hits) => displayed.push(hits), () => undefined);
    await vi.advanceTimersByTimeAsync(150);
    const hit = { path: "new.ts", label: "new.ts", category: "module", language: "typescript" };
    resolveNew([hit]);
    await Promise.resolve();
    resolveOld([{ path: "old.ts", label: "old.ts", category: "module", language: "typescript" }]);
    await Promise.resolve();
    expect(displayed).toEqual([[hit]]);
    cancelNew();
  });
});

describe("map inspector", () => {
  it("keeps focus available without offering collapse for a fixed focused namespace frame", () => {
    const node: FlowNode = { id: "dir:pkg", kind: "folder", label: "pkg", parent: null, detail: { path: "pkg", files: 1, container: true, expanded: true, expandable: false } };
    const graph: FlowGraph = { root: "@project", nodes: [node], edges: [] };
    const html = renderToStaticMarkup(createElement(MapDetails, { node, graph, onToggle: () => undefined, onFocus: () => undefined, onEnter: () => undefined, onClose: () => undefined }));
    expect(html).toContain("Focus folder");
    expect(html).not.toContain("Collapse folder");
    expect(html).not.toContain("Expand folder");
  });

  it.each([true, undefined])("keeps collapse available for an ordinary expanded folder (expandable=%s)", (expandable) => {
    const node: FlowNode = { id: "dir:src", kind: "folder", label: "src", parent: null, detail: { path: "src", files: 1, container: true, expanded: true, expandable } };
    const graph: FlowGraph = { root: "@project", nodes: [node], edges: [] };
    const html = renderToStaticMarkup(createElement(MapDetails, { node, graph, onToggle: () => undefined, onFocus: () => undefined, onEnter: () => undefined, onClose: () => undefined }));
    expect(html).toContain("Collapse folder");
  });

  it("shows incident dependency confidence and evidence alongside supported function links", () => {
    const node: FlowNode = { id: "file:main.py", kind: "module", label: "main.py", parent: null, detail: { path: "main.py", category: "module", language: "python", analysis: "semantic", readable: true, functions: [{ id: "main.py::main", label: "main", line: 7 }] } };
    const graph: FlowGraph = {
      root: "@project", nodes: [node, { id: "external:typing", kind: "external", label: "typing", parent: null, detail: {} }],
      edges: [{ source: node.id, target: "external:typing", kind: "dependency", label: "", data: [], confidence: "verified", count: 2, evidence: [{ file: "main.py", line: 1, text: "from typing import Any" }] }],
    };
    const html = renderToStaticMarkup(createElement(MapDetails, { node, graph, onToggle: () => undefined, onFocus: () => undefined, onEnter: () => undefined, onClose: () => undefined }));
    expect(html).toContain("Verified");
    expect(html).toContain("from typing import Any");
    expect(html).toContain("main.py:1");
    expect(html).toContain("Open flow");
    expect(html).not.toContain("Simulate from here");
  });

  it("describes a parsed file without functions without promising a function flow", () => {
    const node: FlowNode = { id: "file:pkg/__init__.py", kind: "module", label: "__init__.py", parent: null, detail: { path: "pkg/__init__.py", language: "python", analysis: "semantic", functions: [], readable: true } };
    const graph: FlowGraph = { root: "@project", nodes: [node], edges: [] };
    const html = renderToStaticMarkup(createElement(MapDetails, { node, graph, onToggle: () => undefined, onFocus: () => undefined, onEnter: () => undefined, onClose: () => undefined }));
    expect(html).toContain("Parsed source");
    expect(html).not.toContain("Open flow");
  });

  it("loads a bounded map source preview without publishing a response after cancellation", async () => {
    const requests: string[] = [];
    let finish!: (response: Response) => void;
    vi.stubGlobal("fetch", (url: string) => { requests.push(url); return new Promise<Response>((resolve) => { finish = resolve; }); });
    const displayed: SourceLines[] = [];
    const cancel = requestMapSource("docs/hello μ.md", (source) => displayed.push(source), () => undefined);
    cancel();
    finish(new Response(JSON.stringify({ file: "docs/hello μ.md", start: 1, lines: ["# Hello"] })));
    await vi.waitFor(() => expect(requests).toEqual(["api/map-source?file=docs%2Fhello+%CE%BC.md&start=1&end=60"]));
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(displayed).toEqual([]);
  });
});

describe("project map bookmarks", () => {
  it("round trips focused paths and stable folder IDs with spaces and Unicode", () => {
    const state: ViewState = { mode: "map", root: "@project", path: "src/订单 util.ts", expanded: ["dir:src", "dir:src/订单 helpers"] };
    writeHash(state, "push");
    expect(readHash()).toEqual(state);
    const params = new URLSearchParams(location.hash.slice(1));
    expect(params.get("mode")).toBe("map");
    expect(params.get("path")).toBe("src/订单 util.ts");
    expect(params.has("view")).toBe(false);
  });

  it("keeps comma and percent characters in individual map folder IDs", () => {
    const state: ViewState = { mode: "map", root: "@project", path: "lib,common/100% util μ.ts", expanded: ["dir:lib,common", "dir:100% helpers μ"] };
    writeHash(state, "replace");
    expect(readHash()).toEqual(state);
    const params = new URLSearchParams(location.hash.slice(1));
    expect(params.getAll("open")).toEqual(["dir:lib,common", "dir:100% helpers μ"]);
    expect(params.has("expand")).toBe(false);
  });

  it("opens an explicit project map bookmark without a function root", () => {
    location.hash = "#mode=map&path=docs%2Fgetting+started.md&expand=dir%3Adocs";
    expect(readHash()).toEqual({ mode: "map", root: "@project", path: "docs/getting started.md", expanded: ["dir:docs"] });
  });

  it("preserves existing function and trail bookmarks", () => {
    location.hash = "#root=src%2Fshop.py%3A%3Aorder&start=Place+order&expand=s%2F1%2Cs%2F2&at=s%2F1&var=items&view=steps";
    const state = readHash();
    expect(state).toEqual({ root: "src/shop.py::order", start: "Place order", expanded: ["s/1", "s/2"], at: "s/1", var: "items", trailView: "steps" });
    writeHash(state!, "replace");
    expect(readHash()).toEqual(state);
    expect(new URLSearchParams(location.hash.slice(1)).has("mode")).toBe(false);
  });
});

describe("map and flow API routing", () => {
  it("requests a focused overview and exports the same projection", async () => {
    const requests: string[] = [];
    const graph = { root: "@project", nodes: [], edges: [], overview: { path: "src/util μ.ts", files: 1, total_files: 3, parsed_files: 0, relationships: 0, languages: { TypeScript: 1 }, truncated: false } };
    vi.stubGlobal("fetch", async (url: string) => {
      requests.push(url);
      return url.startsWith("api/mermaid") ? new Response("flowchart LR") : new Response(JSON.stringify(graph));
    });
    const view: ViewState = { mode: "map", root: "@project", path: "src/util μ.ts", expanded: ["dir:src", "dir:src/nested"] };
    expect(await api.flow(view)).toEqual(graph);
    expect(await api.mermaid(view, "LR")).toBe("flowchart LR");
    expect(requests).toEqual([
      "api/overview?path=src%2Futil+%CE%BC.ts&open=dir%3Asrc&open=dir%3Asrc%2Fnested",
      "api/mermaid?mode=map&path=src%2Futil+%CE%BC.ts&open=dir%3Asrc&open=dir%3Asrc%2Fnested&dir=LR",
    ]);
  });

  it("keeps function and data trail requests on their existing endpoints", async () => {
    const requests: string[] = [];
    vi.stubGlobal("fetch", async (url: string) => { requests.push(url); return new Response(JSON.stringify({ root: "app.py::main", nodes: [], edges: [] })); });
    await api.flow({ root: "app.py::main", expanded: ["s/1"] });
    await api.flow({ root: "app.py::main", expanded: [], at: "s/1", var: "input" });
    expect(requests).toEqual([
      "api/flow?root=app.py%3A%3Amain&expand=s%2F1",
      "api/trail?root=app.py%3A%3Amain&at=s%2F1&var=input&view=journey",
    ]);
  });
});
