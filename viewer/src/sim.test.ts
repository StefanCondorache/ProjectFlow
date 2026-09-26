import { expect, it } from "vitest";
import type { Placed } from "./layout";
import { badgeFor, pathLength, pointAt, restingPoint, tokenPath, type SimFrame } from "./sim";

const placed: Placed = {
  width: 400,
  height: 200,
  nodes: [
    { node: { id: "s", kind: "start", label: "main", parent: null, detail: {} }, x: 0, y: 80, width: 60, height: 40, depth: 0 },
    { node: { id: "g", kind: "group", label: "load", parent: null, detail: {} }, x: 100, y: 40, width: 200, height: 120, depth: 0 },
    { node: { id: "g/s", kind: "start", label: "load", parent: "g", detail: { inner: true } }, x: 120, y: 90, width: 40, height: 20, depth: 1 },
    { node: { id: "e", kind: "end", label: "END", parent: null, detail: {} }, x: 340, y: 80, width: 60, height: 40, depth: 0 },
  ],
  edges: [
    { id: "e0", edge: { source: "s", target: "g", label: "", kind: "flow", data: [] }, points: [{ x: 60, y: 100 }, { x: 100, y: 100 }] },
    { id: "e1", edge: { source: "g", target: "e", label: "", kind: "flow", data: [] }, points: [{ x: 300, y: 100 }, { x: 340, y: 100 }] },
  ],
};

const frame = (node: string, edge: [string, string] | null, hop = false): SimFrame => ({
  node,
  edge,
  hop,
  note: "",
  stack: [],
  changes: { added: [], changed: [], removed: [] },
});

it("rests at the side of a box the flow comes in by, so labels stay readable", () => {
  expect(restingPoint(placed, frame("s", null), null, "RIGHT")).toEqual({ x: 0, y: 100 });
  expect(restingPoint(placed, frame("s", null), null, "DOWN")).toEqual({ x: 30, y: 80 });
  expect(restingPoint(placed, frame("g", ["s", "g"]), frame("s", null), "RIGHT")).toEqual({ x: 100, y: 100 });
  expect(restingPoint(placed, frame("g", ["s", "g"]), frame("s", null), "DOWN")).toEqual({ x: 200, y: 40 });
});

it("leaves an opened step through its far side", () => {
  const inside = frame("g/s", ["g", "g/s"], true);
  expect(restingPoint(placed, frame("g", ["g/s", "g"], true), inside, "RIGHT")).toEqual({ x: 300, y: 100 });
  expect(restingPoint(placed, frame("g", ["g/s", "g"], true), inside, "DOWN")).toEqual({ x: 200, y: 160 });
});

it("follows the drawn arrow up to the box it reaches", () => {
  expect(tokenPath(placed, frame("e", ["g", "e"]), frame("g", null), { x: 300, y: 100 }, "RIGHT")).toEqual([
    { x: 300, y: 100 },
    { x: 340, y: 100 },
  ]);
});

it("jumps straight across a hop, starting where the token is", () => {
  expect(tokenPath(placed, frame("g/s", ["g", "g/s"], true), frame("g", ["s", "g"]), { x: 100, y: 100 }, "RIGHT")).toEqual([
    { x: 100, y: 100 },
    { x: 120, y: 100 },
  ]);
});

it("walks a path by length and gives the heading in degrees", () => {
  const path = [
    { x: 0, y: 0 },
    { x: 10, y: 0 },
    { x: 10, y: 10 },
  ];
  expect(pathLength(path)).toBe(20);
  expect(pointAt(path, 5)).toEqual({ x: 5, y: 0, angle: 0 });
  expect(pointAt(path, 15)).toEqual({ x: 10, y: 5, angle: 90 });
  expect(pointAt(path, 99)).toEqual({ x: 10, y: 10, angle: 90 });
});

it("the badge names what the token carries, or what just changed", () => {
  const carrying = { ...frame("s", null), stack: [{ function: "f", label: "f", file: "m.py", vars: { parser: { t: "?" as const, from: "p()" }, args: { t: "val" as const, v: 1 } } }] };
  expect(badgeFor(carrying)).toBe("parser · args");
  expect(badgeFor({ ...carrying, changes: { added: ["cfg.mode", "x"], changed: [], removed: [] } })).toBe("+ cfg.mode +1");
});
