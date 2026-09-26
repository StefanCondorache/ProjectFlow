// Flow graph -> ELK input, and ELK output -> absolutely positioned boxes.
//
// ELK's layered algorithm is the same family Mermaid uses, so the result reads
// top-down like a Mermaid flowchart. Frames (loops, try blocks, opened steps)
// are compound nodes; every edge is declared at the root and ELK routes it
// across frame borders. Coordinates come back absolute ("ROOT" mode).
import type { ElkExtendedEdge, ElkNode, LayoutOptions } from "elkjs/lib/elk-api";
import type { FlowEdge, FlowGraph, FlowNode } from "./types";

export const FRAME_KINDS = new Set(["loop", "try", "handler", "group", "file"]);

export interface Size {
  width: number;
  height: number;
}

export interface Sizing {
  node: (node: FlowNode) => Size;
  label: (text: string) => Size;
  header: (node: FlowNode) => number;
}

export interface Point {
  x: number;
  y: number;
}

export interface PlacedNode extends Size, Point {
  node: FlowNode;
  depth: number;
}

export interface PlacedEdge {
  id: string;
  edge: FlowEdge;
  points: Point[];
  label?: Size & Point & { text: string };
}

export interface Placed extends Size {
  nodes: PlacedNode[];
  edges: PlacedEdge[];
}

const ROOT_OPTIONS: LayoutOptions = {
  "elk.algorithm": "layered",
  "elk.direction": "DOWN",
  "elk.hierarchyHandling": "INCLUDE_CHILDREN",
  "elk.edgeRouting": "ORTHOGONAL",
  "elk.json.shapeCoords": "ROOT",
  "elk.json.edgeCoords": "ROOT",
  "elk.edgeLabels.placement": "CENTER",
  "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
  "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
  "elk.layered.nodePlacement.bk.fixedAlignment": "BALANCED",
  "elk.spacing.nodeNode": "32",
  "elk.layered.spacing.nodeNodeBetweenLayers": "40",
  "elk.spacing.edgeNode": "18",
  "elk.layered.spacing.edgeNodeBetweenLayers": "18",
  "elk.spacing.edgeEdge": "12",
  "elk.spacing.edgeLabel": "4",
  "elk.padding": "[top=24,left=24,bottom=24,right=24]",
};

function frameOptions(headerWidth: number): LayoutOptions {
  return {
    "elk.padding": "[top=38,left=18,bottom=18,right=18]",
    "elk.nodeSize.constraints": "[MINIMUM_SIZE]",
    "elk.nodeSize.minimum": `(${Math.ceil(headerWidth)}, 60)`,
  };
}

export function toElk(graph: FlowGraph, sizing: Sizing): ElkNode {
  const root: ElkNode = { id: "__root", layoutOptions: ROOT_OPTIONS, children: [], edges: [] };
  const elkNodes = new Map<string, ElkNode>();
  for (const node of graph.nodes) {
    elkNodes.set(
      node.id,
      FRAME_KINDS.has(node.kind)
        ? { id: node.id, children: [], layoutOptions: frameOptions(sizing.header(node)) }
        : { id: node.id, ...sizing.node(node) },
    );
  }
  for (const node of graph.nodes) {
    const parent = (node.parent && elkNodes.get(node.parent)) || root;
    parent.children!.push(elkNodes.get(node.id)!);
  }
  root.edges = graph.edges.map(
    (edge, i): ElkExtendedEdge => ({
      id: `e${i}`,
      sources: [edge.source],
      targets: [edge.target],
      labels: edge.label ? [{ text: edge.label, ...sizing.label(edge.label) }] : [],
    }),
  );
  return root;
}

export function place(result: ElkNode, graph: FlowGraph): Placed {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const depthOf = (node: FlowNode): number => {
    let depth = 0;
    for (let p = node.parent; p; p = byId.get(p)?.parent ?? null) depth++;
    return depth;
  };
  const nodes: PlacedNode[] = [];
  const visit = (elk: ElkNode) => {
    for (const child of elk.children ?? []) {
      const node = byId.get(child.id);
      if (node) {
        nodes.push({ node, x: child.x ?? 0, y: child.y ?? 0, width: child.width ?? 0, height: child.height ?? 0, depth: depthOf(node) });
      }
      visit(child);
    }
  };
  visit(result);

  const edges: PlacedEdge[] = (result.edges ?? []).map((elk) => {
    const index = Number(elk.id.slice(1));
    const section = (elk as ElkExtendedEdge).sections?.[0];
    const points = section ? [section.startPoint, ...(section.bendPoints ?? []), section.endPoint].map(({ x, y }) => ({ x, y })) : [];
    const lab = elk.labels?.[0];
    const label = lab && lab.text ? { text: lab.text, x: lab.x ?? 0, y: lab.y ?? 0, width: lab.width ?? 0, height: lab.height ?? 0 } : undefined;
    return { id: elk.id, edge: graph.edges[index], points, ...(label ? { label } : {}) };
  });
  return { width: result.width ?? 0, height: result.height ?? 0, nodes, edges };
}

/** An orthogonal polyline with its corners rounded off. */
export function roundedPath(points: Point[], radius = 6): string {
  if (points.length === 0) return "";
  const fmt = (p: Point) => `${round(p.x)},${round(p.y)}`;
  const parts = [`M${fmt(points[0])}`];
  for (let i = 1; i < points.length - 1; i++) {
    const [prev, corner, next] = [points[i - 1], points[i], points[i + 1]];
    const r = Math.min(radius, dist(prev, corner) / 2, dist(corner, next) / 2);
    parts.push(`L${fmt(towards(corner, prev, r))}`, `Q${fmt(corner)} ${fmt(towards(corner, next, r))}`);
  }
  parts.push(`L${fmt(points[points.length - 1])}`);
  return parts.join(" ");
}

function dist(a: Point, b: Point): number {
  return Math.hypot(b.x - a.x, b.y - a.y);
}

function towards(from: Point, to: Point, length: number): Point {
  const d = dist(from, to) || 1;
  return { x: from.x + ((to.x - from.x) / d) * length, y: from.y + ((to.y - from.y) / d) * length };
}

function round(v: number): number {
  return Math.round(v * 10) / 10;
}
