// Simulation data from the server, and the route of the token that carries it.
import type { Direction, Placed, Point } from "./layout";

export type SimValue =
  | { t: "val"; v: string | number | boolean | null; type?: string }
  | { t: "dict"; v: Record<string, SimValue>; more?: number }
  | { t: "list"; v: SimValue[]; kind?: "tuple" | "set"; more?: number }
  | { t: "obj"; cls: string; v: Record<string, SimValue> }
  | { t: "ref"; v: string }
  | { t: "?"; from: string; v?: Record<string, SimValue> };

export interface SimScope {
  function: string;
  label: string;
  file: string;
  vars: Record<string, SimValue>;
}

export interface SimChanges {
  added: string[];
  changed: string[];
  removed: string[];
}

/** Something the program writes, prints or logs: recorded, never done. */
export interface SimOutput {
  target: string;
  how: string;
  data: SimValue;
}

export interface SimError {
  type: string;
  message: string;
  file: string;
  line: number;
}

export interface SimFrame {
  node: string;
  edge: [string, string] | null;
  hop: boolean;
  note: string;
  stack: SimScope[];
  changes: SimChanges;
  outputs: SimOutput[];
  error: SimError | null;
}

export interface SimChoice {
  /** The node asking, plus "@round" per loop round when a loop asks again each time. */
  node: string;
  question: string;
  options: { label: string; value: string }[];
}

export interface Simulation {
  status: "done" | "choose" | "raised" | "limit" | "unreached";
  choice: SimChoice | null;
  error: SimError | null;
  /** The opened steps the frames refer to (more than asked for when every step is gone into). */
  expanded: string[];
  frames: SimFrame[];
}

/** What a run is given, besides the answers to its questions. */
export interface SimSetup {
  /** Node the run starts at (null: the start of the diagram). */
  at: string | null;
  inputs: Record<string, unknown>;
  env: Record<string, string | null>;
  argv: string[] | null;
  provided: Record<string, unknown>;
  autoOpen: boolean;
}

export const NO_SETUP: SimSetup = { at: null, inputs: {}, env: {}, argv: null, provided: {}, autoOpen: false };

/** What there is at a node for a run started there. */
export interface Expects {
  reached: boolean;
  function?: string;
  label?: string;
  file?: string;
  vars: Record<string, { value: SimValue; known: boolean; type: string | null; template: unknown }>;
  env: string[];
  argv: boolean;
}

/** A shown value back as JSON, to edit; undefined when it is not known. */
export function plainOf(value: SimValue): unknown {
  switch (value.t) {
    case "val":
      return value.v;
    case "dict":
    case "obj":
      return Object.fromEntries(Object.entries(value.v).map(([k, v]) => [k, plainOf(v)]));
    case "list":
      return value.v.map(plainOf);
    default:
      return undefined;
  }
}

/** A command line split like a shell would: quotes keep spaces. */
export function splitArgs(text: string): string[] {
  const out: string[] = [];
  let current = "";
  let quote: string | null = null;
  let started = false;
  for (const ch of text) {
    if (quote) {
      if (ch === quote) quote = null;
      else current += ch;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      started = true;
    } else if (/\s/.test(ch)) {
      if (started) out.push(current);
      current = "";
      started = false;
    } else {
      current += ch;
      started = true;
    }
  }
  if (started) out.push(current);
  return out;
}

export function joinArgs(argv: string[]): string {
  return argv.map((a) => (a === "" || /[\s"']/.test(a) ? JSON.stringify(a) : a)).join(" ");
}

/** What was typed: JSON when it parses, else the text itself. */
export function readInput(text: string): { value: unknown; text: boolean } {
  try {
    return { value: JSON.parse(text), text: false };
  } catch {
    return { value: text, text: true };
  }
}

const FRAMES = new Set(["group", "loop", "try", "handler", "file"]);

/** What the token's badge says: what just changed, else what it carries. */
export function badgeFor(frame: SimFrame): string {
  const { added, changed } = frame.changes;
  const more = (list: string[]) => (list.length > 1 ? ` +${list.length - 1}` : "");
  if (added.length) return `+ ${added[0]}${more(added)}`;
  if (changed.length) return `~ ${changed[0]}${more(changed)}`;
  const names = Object.keys(frame.stack[frame.stack.length - 1]?.vars ?? {});
  if (!names.length) return "no data yet";
  return names.slice(0, 3).join(" · ") + (names.length > 3 ? ` +${names.length - 3}` : "");
}

/** Where the token stops for a frame: at the side of the box the flow comes
 * in by (so the box's label stays readable), or the far side of a box it is
 * leaving. */
export function restingPoint(placed: Placed, frame: SimFrame, previous: SimFrame | null, direction: Direction): Point | null {
  const box = placed.nodes.find((p) => p.node.id === frame.node);
  if (!box) return null;
  const leaving = FRAMES.has(box.node.kind) && previous !== null && previous.node.startsWith(`${frame.node}/`);
  if (direction === "RIGHT") {
    return { x: leaving ? box.x + box.width : box.x, y: box.y + box.height / 2 };
  }
  return { x: box.x + box.width / 2, y: leaving ? box.y + box.height : box.y };
}

/** The route to a frame's node: along the drawn arrow when there is one,
 * otherwise straight from where the token is. */
export function tokenPath(placed: Placed, frame: SimFrame, previous: SimFrame | null, from: Point | null, direction: Direction): Point[] {
  const end = restingPoint(placed, frame, previous, direction);
  if (!end) return [];
  if (frame.edge && !frame.hop) {
    const [source, target] = frame.edge;
    const drawn = placed.edges.find((e) => e.edge.source === source && e.edge.target === target);
    if (drawn && drawn.points.length > 1) return drawn.points;
  }
  return from ? [from, end] : [end];
}

export function pathLength(points: Point[]): number {
  let total = 0;
  for (let i = 1; i < points.length; i++) total += Math.hypot(points[i].x - points[i - 1].x, points[i].y - points[i - 1].y);
  return total;
}

/** The point ``distance`` along a path, with the heading of travel in degrees. */
export function pointAt(points: Point[], distance: number): { x: number; y: number; angle: number } {
  if (points.length === 1) return { ...points[0], angle: 0 };
  let left = Math.max(0, distance);
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1];
    const b = points[i];
    const length = Math.hypot(b.x - a.x, b.y - a.y);
    const angle = Math.round((Math.atan2(b.y - a.y, b.x - a.x) * 180) / Math.PI);
    if (left <= length || i === points.length - 1) {
      const t = length ? Math.min(1, left / length) : 1;
      return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t, angle };
    }
    left -= length;
  }
  return { ...points[points.length - 1], angle: 0 };
}
