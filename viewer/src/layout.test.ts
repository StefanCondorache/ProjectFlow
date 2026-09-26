import type { ElkNode } from "elkjs/lib/elk-api";
import { expect, it } from "vitest";
import { place, roundedPath, toElk } from "./layout";
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
