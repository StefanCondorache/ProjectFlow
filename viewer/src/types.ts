export interface Source {
  kind: string;
  text: string;
}

export interface Entry {
  target: string;
  label: string;
  kind: string;
  sources: Source[];
  reach: number;
}

export interface ProjectInfo {
  name: string;
  root: string;
  files: string[];
  stats: { files: number; functions: number; classes: number; calls: number; project_calls: number };
  entries: Entry[];
}

export type NodeKind =
  | "start"
  | "end"
  | "step"
  | "io"
  | "use"
  | "decision"
  | "loop"
  | "try"
  | "handler"
  | "raise"
  | "group"
  | "file";

export interface Detail {
  target?: string | null;
  cls?: string | null;
  function?: string;
  file?: string | null;
  line?: number;
  def_line?: number;
  doc?: string | null;
  text?: string;
  confidence?: string;
  expandable?: boolean;
  recursive?: boolean;
  args?: string[];
  defs?: string[];
  params?: string[];
  io?: [string, string];
  external?: string | null;
  inner?: boolean;
  at?: string;
  loop?: string;
  trail?: "origin" | "on";
  receives?: string[];
  returns?: boolean;
}

export interface FlowNode {
  id: string;
  kind: NodeKind;
  label: string;
  parent: string | null;
  detail: Detail;
}

export interface FlowEdge {
  source: string;
  target: string;
  label: string;
  kind: "flow" | "yes" | "no" | "case" | "error" | "return";
  /** Variables behind a data label ("$1" is a nested call's result). */
  data: string[];
}

export interface TrailInfo {
  origin: string;
  var: string;
  truncated: boolean;
  view: "journey" | "steps";
}

export interface FlowGraph {
  root: string;
  nodes: FlowNode[];
  edges: FlowEdge[];
  trail?: TrailInfo;
}

export interface SourceLines {
  file: string;
  start: number;
  lines: string[];
}
