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
  inventory?: { files: number; parsed_files: number; languages: Record<string, number> };
  file_languages?: Record<string, string>;
  capabilities?: { overview: boolean; flow_languages: string[]; trail_languages?: string[]; simulation_languages: string[] };
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
  | "file"
  | "folder"
  | "module"
  | "config"
  | "document"
  | "asset"
  | "external";

export interface MapDetail {
  path?: string;
  category?: string;
  language?: string;
  files?: number;
  size?: number;
  readable?: boolean;
  analysis?: string;
  container?: boolean;
  expanded?: boolean;
  expandable?: boolean;
  functions?: { id: string; label: string; line: number }[];
  diagnostics?: { code: string; message: string; line: number; col: number; severity: string }[];
}

export interface Detail extends MapDetail {
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
  kind: "flow" | "yes" | "no" | "case" | "error" | "return" | "dependency";
  /** Variables behind a data label; generated_data identifies synthetic results. */
  data: string[];
  generated_data?: string[] | null;
  count?: number;
  confidence?: "verified" | "inferred";
  evidence?: Evidence[];
}

export interface Evidence {
  file: string;
  line: number;
  text: string;
}

export interface OverviewInfo {
  path: string;
  files: number;
  total_files: number;
  parsed_files: number;
  relationships: number;
  languages: Record<string, number>;
  truncated: boolean;
}

export interface MapSearchResult {
  path: string;
  label: string;
  category: string;
  language: string;
}

export interface MapChildrenPage {
  path: string;
  items: MapSearchResult[];
  total: number;
  offset: number;
  limit: number;
}

export interface TrailInfo {
  origin: string;
  var: string;
  generated?: boolean | null;
  truncated: boolean;
  view: "journey" | "steps";
}

export interface FlowGraph {
  root: string;
  nodes: FlowNode[];
  edges: FlowEdge[];
  trail?: TrailInfo;
  overview?: OverviewInfo;
}

export interface SourceLines {
  file: string;
  start: number;
  lines: string[];
}
