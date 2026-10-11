import type { ElkNode } from "elkjs/lib/elk-api";
import ELK from "elkjs/lib/elk.bundled.js";
import { expect, it } from "vitest";
import { isFrame, place, roundedPath, toElk } from "./layout";
import { nodeSize } from "./measure";
import type { FlowGraph } from "./types";

const graph: FlowGraph = {
  root: "m.py::main",
  nodes: [
    { id: "s", kind: "start", label: "main", parent: null, detail: {} },
    { id: "n1", kind: "group", label: "inner", parent: null, detail: { file: "m.py" } },
    { id: "n1/s", kind: "start", label: "inner", parent: "n1", detail: { inner: true } },
    { id: "n1/n1", kind: "step", label: "helper", parent: "n1", detail: { file: "m.py" } },
    { id: "n1/e", kind: "end", label: "END", parent: "n1", detail: { inner: true } },
    { id: "e", kind: "end", label: "END", parent: null, detail: {} },
  ],
  edges: [
    { source: "s", target: "n1", label: "", kind: "flow", data: [] },
    { source: "n1/s", target: "n1/n1", label: "x", kind: "flow", data: ["x"] },
    { source: "n1/n1", target: "n1/e", label: "", kind: "flow", data: [] },
    { source: "n1", target: "e", label: "", kind: "flow", data: [] },
  ],
};

const sizing = {
  node: () => ({ width: 100, height: 40 }),
  label: (text: string) => ({ width: text.length * 7 + 8, height: 16 }),
  header: () => 120,
};

it("nests frame children under their parent and sizes only the leaves", () => {
  const elk = toElk(graph, sizing);
  expect(elk.layoutOptions!["elk.hierarchyHandling"]).toBe("INCLUDE_CHILDREN");
  expect(elk.children!.map((c) => c.id)).toEqual(["s", "n1", "e"]);
  const group = elk.children![1];
  expect(group.children!.map((c) => c.id)).toEqual(["n1/s", "n1/n1", "n1/e"]);
  expect(group.width).toBeUndefined();
  expect(elk.children![0]).toMatchObject({ width: 100, height: 40 });
});

it("declares every edge at the root, with sized labels", () => {
  const elk = toElk(graph, sizing);
  expect(elk.edges!.map((e) => [e.sources[0], e.targets[0]])).toEqual([
    ["s", "n1"],
    ["n1/s", "n1/n1"],
    ["n1/n1", "n1/e"],
    ["n1", "e"],
  ]);
  expect(elk.edges![1].labels).toEqual([{ text: "x", width: 15, height: 16 }]);
  expect(elk.edges![0].labels).toEqual([]);
});

it("place() gives absolute boxes with nesting depth and edges as point lists", () => {
  const result: ElkNode = {
    id: "__root",
    width: 300,
    height: 400,
    children: [
      { id: "s", x: 10, y: 10, width: 100, height: 40 },
      { id: "n1", x: 5, y: 80, width: 200, height: 200, children: [{ id: "n1/n1", x: 20, y: 120, width: 100, height: 40 }] },
    ],
    edges: [
      {
        id: "e0",
        sources: ["s"],
        targets: ["n1"],
        sections: [{ id: "x", startPoint: { x: 60, y: 50 }, bendPoints: [{ x: 60, y: 60 }], endPoint: { x: 60, y: 80 } }],
        labels: [{ text: "cfg", x: 64, y: 55, width: 29, height: 16 }],
      },
    ],
  };
  const placed = place(result, graph);
  expect(placed.nodes.map((p) => [p.node.id, p.x, p.y, p.depth])).toEqual([
    ["s", 10, 10, 0],
    ["n1", 5, 80, 0],
    ["n1/n1", 20, 120, 1],
  ]);
  expect(placed.edges[0].edge).toBe(graph.edges[0]);
  expect(placed.edges[0].points).toEqual([
    { x: 60, y: 50 },
    { x: 60, y: 60 },
    { x: 60, y: 80 },
  ]);
  expect(placed.edges[0].label).toEqual({ text: "cfg", x: 64, y: 55, width: 29, height: 16 });
  expect([placed.width, placed.height]).toEqual([300, 400]);
});

it("roundedPath keeps straight runs straight and rounds the corners", () => {
  expect(roundedPath([{ x: 0, y: 0 }, { x: 0, y: 10 }], 4)).toBe("M0,0 L0,10");
  expect(roundedPath([{ x: 0, y: 0 }, { x: 0, y: 10 }, { x: 10, y: 10 }], 4)).toBe("M0,0 L0,6 Q0,10 4,10 L10,10");
  expect(roundedPath([{ x: 0, y: 0 }, { x: 0, y: 4 }, { x: 10, y: 4 }], 4)).toBe("M0,0 L0,2 Q0,4 2,4 L10,4");
});

it("lays the flow out left to right unless asked for top-down", () => {
  expect(toElk(graph, sizing).layoutOptions!["elk.direction"]).toBe("RIGHT");
  expect(toElk(graph, sizing, "DOWN").layoutOptions!["elk.direction"]).toBe("DOWN");
});

it("keeps a collapsed folder as a sized leaf and an expanded folder as a compound frame", async () => {
  const map: FlowGraph = {
    root: "@project",
    overview: { path: "", files: 6, total_files: 6, parsed_files: 0, relationships: 1, languages: {}, truncated: false },
    nodes: [
      { id: "dir:docs", kind: "folder", label: "docs", parent: null, detail: { files: 5 } },
      { id: "dir:src", kind: "folder", label: "src", parent: null, detail: { container: true } },
      { id: "file:src/app.ts", kind: "module", label: "app.ts", parent: "dir:src", detail: {} },
    ],
    edges: [{ source: "file:src/app.ts", target: "dir:docs", kind: "dependency", label: "", data: [] }],
  };
  expect(isFrame(map.nodes[0])).toBe(false);
  expect(isFrame(map.nodes[1])).toBe(true);
  const elk = toElk(map, { ...sizing, node: nodeSize });
  expect(elk.layoutOptions!["elk.hierarchyHandling"]).toBe("INCLUDE_CHILDREN");
  expect(elk.children![0]).toMatchObject({ width: 240, height: 90 });
  expect(elk.children![0].children).toBeUndefined();
  expect(elk.children![1].width).toBeUndefined();
  expect(elk.children![1].children![0]).toMatchObject({ id: "file:src/app.ts", width: 240, height: 90 });
  expect(isFrame(graph.nodes[1])).toBe(true);
  expect(isFrame({ ...graph.nodes[1], kind: "file" })).toBe(true);
  const result = await new ELK().layout(elk);
  const frame = result.children!.find((node) => node.id === "dir:src")!;
  expect(frame.children).toHaveLength(1);
  expect(frame.width).toBeGreaterThan(frame.children![0].width!);
  expect(frame.height).toBeGreaterThan(frame.children![0].height!);
});

it("reserves readable cards for every neutral file category", () => {
  for (const kind of ["module", "config", "document", "asset", "external"] as const) {
    expect(nodeSize({ id: kind, kind, label: "a-long-file-name.unknown", parent: null, detail: {} })).toEqual({ width: 240, height: 90 });
  }
});

it("shows aggregate dependency counts even when the API omits a text label", () => {
  const elk = toElk({ ...graph, edges: [{ source: "s", target: "e", label: "", kind: "dependency", data: [], count: 4, confidence: "inferred" }] }, sizing);
  expect(elk.edges![0].labels).toEqual([{ text: "4 imports", width: 71, height: 16 }]);
});

it("packs connected and disconnected flat map cards into a readable landscape overview", async () => {
  const map: FlowGraph = {
    root: "@project",
    overview: { path: "", files: 10, total_files: 10, parsed_files: 0, relationships: 3, languages: {}, truncated: false },
    nodes: Array.from({ length: 10 }, (_, index) => ({ id: `file:${index}.ts`, kind: "module", label: `${index}.ts`, parent: null, detail: {} })),
    edges: [[0, 1], [0, 2], [3, 4]].map(([source, target]) => ({ source: `file:${source}.ts`, target: `file:${target}.ts`, label: "", kind: "dependency", data: [] })),
  };
  const result = await new ELK().layout(toElk(map, { ...sizing, node: nodeSize }));
  expect(result.children).toHaveLength(10);
  expect(result.edges).toHaveLength(3);
  const aspectRatio = result.width! / result.height!;
  expect(aspectRatio).toBeGreaterThan(1);
  expect(aspectRatio).toBeLessThan(2.5);
});

it("packs a large dependency-free expanded folder while keeping its cards inside the frame", async () => {
  const map: FlowGraph = {
    root: "@project",
    overview: { path: "", files: 240, total_files: 240, parsed_files: 0, relationships: 0, languages: {}, truncated: true },
    nodes: [
      { id: "dir:wide", kind: "folder", label: "wide", parent: null, detail: { container: true, files: 240 } },
      ...Array.from({ length: 199 }, (_, index) => ({ id: `file:wide/${index}.txt`, kind: "document" as const, label: `${index}.txt`, parent: "dir:wide", detail: {} })),
    ],
    edges: [],
  };
  const result = await new ELK().layout(toElk(map, { ...sizing, node: nodeSize }));
  const frame = result.children![0];
  expect(frame.children).toHaveLength(199);
  expect(frame.width! / frame.height!).toBeGreaterThan(0.8);
  expect(frame.width! / frame.height!).toBeLessThan(2.5);
  for (const card of frame.children!) {
    expect(card.x).toBeGreaterThanOrEqual(frame.x!);
    expect(card.y).toBeGreaterThanOrEqual(frame.y!);
    expect(card.x! + card.width!).toBeLessThanOrEqual(frame.x! + frame.width!);
    expect(card.y! + card.height!).toBeLessThanOrEqual(frame.y! + frame.height!);
  }
});
