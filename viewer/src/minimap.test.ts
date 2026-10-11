import { expect, it } from "vitest";
import { overviewPoint, overviewTransform, resizeView, revealBox, visibleGraphWindow } from "./minimap";

it("fits a wide graph inside the overview while centering the unused space", () => {
  expect(overviewTransform({ width: 1000, height: 200 }, { width: 176, height: 110 })).toEqual({ scale: 0.16, x: 8, y: 39 });
});

it("maps an overview click back to graph coordinates, including letterboxed space", () => {
  const transform = { scale: 0.16, x: 8, y: 39 };
  const graph = { width: 1000, height: 200 };
  expect(overviewPoint({ x: 88, y: 55 }, transform, graph)).toEqual({ x: 500, y: 100 });
  expect(overviewPoint({ x: 0, y: 110 }, transform, graph)).toEqual({ x: 0, y: 200 });
});

it("shows the visible graph area when the camera is zoomed and panned", () => {
  expect(visibleGraphWindow({ x: -200, y: -100, k: 0.5 }, { width: 300, height: 200 }, { width: 1000, height: 500 })).toEqual({
    x: 400, y: 200, width: 600, height: 300,
  });
});

it("clips the overview window to the graph when the viewport extends past its edges", () => {
  expect(visibleGraphWindow({ x: 100, y: 50, k: 1 }, { width: 800, height: 500 }, { width: 600, height: 300 })).toEqual({
    x: 0, y: 0, width: 600, height: 300,
  });
});

it("reveals a selected node on both axes after the viewport shrinks", () => {
  expect(revealBox({ x: 40, y: 20, k: 0.5 }, { width: 600, height: 400 }, { x: 1200, y: 1000, width: 200, height: 120 })).toEqual({
    x: -124, y: -184, k: 0.5,
  });
});

it("preserves the camera when a selected frame already spans the viewport", () => {
  const view = { x: -100, y: -200, k: 1 };
  expect(revealBox(view, { width: 600, height: 400 }, { x: 0, y: 0, width: 800, height: 800 })).toBe(view);
});

it("preserves the graph point at the viewport center when resizing from desktop to phone", () => {
  expect(resizeView({ x: 200, y: 100, k: 0.7 }, { width: 1200, height: 800 }, { width: 390, height: 700 })).toEqual({
    x: -205, y: 50, k: 0.7,
  });
});

it("keeps the opening camera unchanged for the first viewport measurement", () => {
  const view = { x: 200, y: 100, k: 0.7 };
  expect(resizeView(view, { width: 0, height: 0 }, { width: 1200, height: 800 })).toBe(view);
});
