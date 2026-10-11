import type { Point, Size } from "./layout";

export interface CameraView extends Point {
  k: number;
}

export interface OverviewTransform extends Point {
  scale: number;
}

const clamp = (value: number, limit: number) => Math.max(0, Math.min(limit, value));

/** Uniformly fit the graph inside a compact overview, leaving an eight-pixel inset. */
export function overviewTransform(graph: Size, map: Size): OverviewTransform {
  const scale = Math.min((map.width - 16) / Math.max(graph.width, 1), (map.height - 16) / Math.max(graph.height, 1));
  return { scale, x: (map.width - graph.width * scale) / 2, y: (map.height - graph.height * scale) / 2 };
}

/** Convert an overview click to a graph location; its inset still targets the nearest edge. */
export function overviewPoint(point: Point, transform: OverviewTransform, graph: Size): Point {
  return { x: clamp((point.x - transform.x) / transform.scale, graph.width), y: clamp((point.y - transform.y) / transform.scale, graph.height) };
}

/** The camera's visible rectangle, clipped to the graph rather than its surrounding blank space. */
export function visibleGraphWindow(view: CameraView, viewport: Size, graph: Size): Point & Size {
  const x = clamp(-view.x / view.k, graph.width);
  const y = clamp(-view.y / view.k, graph.height);
  const right = clamp((viewport.width - view.x) / view.k, graph.width);
  const bottom = clamp((viewport.height - view.y) / view.k, graph.height);
  return { x, y, width: right - x, height: bottom - y };
}

/** Reveal both axes, while keeping oversized frames stable when they already cover the viewport. */
export function revealBox(view: CameraView, viewport: Size, box: Point & Size): CameraView {
  const shift = (start: number, length: number, available: number) => {
    const margin = Math.min(24, available / 2);
    const end = start + length;
    if (length > available - 2 * margin) {
      if (start > margin) return margin - start;
      if (end < available - margin) return available - margin - end;
      return 0;
    }
    if (start < margin) return margin - start;
    if (end > available - margin) return available - margin - end;
    return 0;
  };
  const dx = shift(view.x + box.x * view.k, box.width * view.k, viewport.width);
  const dy = shift(view.y + box.y * view.k, box.height * view.k, viewport.height);
  return dx || dy ? { ...view, x: view.x + dx, y: view.y + dy } : view;
}

/** Keep the graph point at the viewport center stable across panel and screen resizes. */
export function resizeView(view: CameraView, before: Size, after: Size): CameraView {
  if (!before.width || !before.height || !after.width || !after.height) return view;
  const dx = (after.width - before.width) / 2;
  const dy = (after.height - before.height) / 2;
  return dx || dy ? { ...view, x: view.x + dx, y: view.y + dy } : view;
}
