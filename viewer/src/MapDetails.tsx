import { useEffect, useRef, useState } from "react";
import { api, languageName } from "./api";
import type { FlowGraph, FlowNode, SourceLines } from "./types";

interface Props {
  node: FlowNode;
  graph: FlowGraph;
  onToggle: (node: FlowNode) => void;
  sourceLine?: number;
  onFocus: (path: string, line?: number) => void;
  onEnter: (id: string) => void;
  onClose: () => void;
}

const CATEGORY: Record<string, string> = { folder: "Folder", module: "Source file", config: "Configuration", document: "Document", asset: "Asset", external: "External dependency" };
const ANALYSIS: Record<string, string> = { imports: "Inferred imports", structural: "File structure" };
const number = new Intl.NumberFormat();

/** A bounded preview uses the inventory endpoint, including non-semantic text files. */
export function requestMapSource(file: string, onResult: (source: SourceLines) => void, onError: () => void, line?: number): () => void {
  let live = true;
  const { start, end } = previewWindow(line);
  api.mapSource(file, start, end).then(
    (source) => live && onResult(source),
    () => live && onError(),
  );
  return () => { live = false; };
}

interface SourceState {
  location: string;
  status: "pending" | "ready" | "error";
  source: SourceLines | null;
}

function previewWindow(line?: number): { start: number; end: number; line?: number } {
  const cited = line !== undefined && Number.isSafeInteger(line) && line > 0 ? line : undefined;
  const start = cited === undefined ? 1 : Math.max(1, cited - 12);
  return { start, end: Math.min(Number.MAX_SAFE_INTEGER, start + 59), line: cited };
}

/** Actual preview markup is shared so a cited row remains accessible and visible. */
export function MapSourcePreview({ source, sourceLine }: { source: SourceLines; sourceLine?: number }) {
  const citedRow = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (sourceLine !== undefined) citedRow.current?.scrollIntoView({ block: "center", inline: "nearest", behavior: "auto" });
  }, [source.file, source.start, source.lines, sourceLine]);
  return <pre className="source" aria-label={`Source from ${source.file}`}><code>{source.lines.map((text, i) => {
    const line = source.start + i;
    const cited = line === sourceLine;
    return <span className={`line${cited ? " is-cited" : ""}`} key={line} ref={cited ? citedRow : undefined} aria-label={cited ? `Cited line ${line}` : undefined} aria-current={cited ? "location" : undefined}><span className="ln">{line}</span>{text || " "}</span>;
  })}</code></pre>;
}

export function MapDetails({ node, graph, sourceLine, onToggle, onFocus, onEnter, onClose }: Props) {
  const d = node.detail;
  const path = d.path;
  const readable = Boolean(path && d.readable && node.kind !== "folder" && node.kind !== "external");
  const preview = previewWindow(sourceLine);
  const sourceKey = readable ? JSON.stringify([path, preview.start, preview.end]) : "";
  const [sourceState, setSourceState] = useState<SourceState>({ location: "", status: "pending", source: null });
  const [attempt, setAttempt] = useState(0);
  const source = sourceState.location === sourceKey ? sourceState.source : null;
  const status = sourceState.location === sourceKey ? sourceState.status : "pending";

  useEffect(() => {
    setSourceState({ location: sourceKey, status: "pending", source: null });
    if (!sourceKey) return;
    return requestMapSource(
      path!,
      (source) => setSourceState({ location: sourceKey, status: "ready", source }),
      () => setSourceState({ location: sourceKey, status: "error", source: null }),
      preview.line,
    );
  }, [sourceKey, attempt]);

  const relationships = graph.edges.filter((edge) => edge.kind === "dependency" && (edge.source === node.id || edge.target === node.id));
  const folder = node.kind === "folder";
  const expanded = Boolean(d.container || d.expanded);
  const analysis = d.analysis === "semantic" ? d.functions?.length ? "Function flows available" : "Parsed source" : ANALYSIS[d.analysis ?? ""] ?? d.analysis;
  return (
    <section className="details map-details" aria-label="Project map details">
      <header className="details-head">
        <div><span className="details-eyebrow">Map inspector</span><span className={`details-kind details-kind-${node.kind}`}>{CATEGORY[node.kind] ?? d.category ?? "Project file"}</span></div>
        <button className="close" onClick={onClose} aria-label="Close details" title="Close (Esc)">×</button>
      </header>
      <h2 className="details-title">{node.label}</h2>
      {node.kind === "external" && <p className="details-doc">Imported from outside this project. Dependencies describe imports, not execution order.</p>}
      <section className="detail-section">
        <h3>File information</h3>
        <dl className="facts">
          {path && <><dt>Path</dt><dd><code>{path}</code></dd></>}
          <dt>Category</dt><dd>{CATEGORY[d.category ?? node.kind] ?? d.category ?? node.kind}</dd>
          {d.language && <><dt>Language</dt><dd>{languageName(d.language)}</dd></>}
          {d.files !== undefined && <><dt>Files</dt><dd>{number.format(d.files)}</dd></>}
          {d.size !== undefined && <><dt>Size</dt><dd>{number.format(d.size)} bytes</dd></>}
          {analysis && <><dt>Analysis</dt><dd>{analysis}</dd></>}
          {!folder && node.kind !== "external" && <><dt>Preview</dt><dd>{d.readable ? "Text preview available" : "No text preview available"}</dd></>}
        </dl>
      </section>
      {path && (
        <section className="detail-section">
          <h3>Explore</h3>
          <div className="actions">
            {folder && d.expandable !== false && (d.expandable || expanded) && <button onClick={() => onToggle(node)}>{expanded ? "Collapse folder" : "Expand folder"}</button>}
            <button onClick={() => onFocus(path)}>Focus {folder ? "folder" : "file"}</button>
          </div>
        </section>
      )}
      {d.functions && d.functions.length > 0 && (
        <section className="detail-section">
          <h3>Function flows</h3>
          <ul className="map-functions">
            {d.functions.map((fn) => <li key={fn.id}><button onClick={() => onEnter(fn.id)} title={`Open ${fn.label} as a function flow`}><code>{fn.label}</code><span>line {fn.line} · Open flow →</span></button></li>)}
          </ul>
        </section>
      )}
      <section className="detail-section">
        <h3>Visible connections <span className="count">{relationships.length}</span></h3>
        {relationships.length === 0 ? <p className="details-doc">No connections are drawn for this visible area. Relationships within a collapsed folder can be folded; expand or browse the folder to explore its files.</p> : (
          <ul className="map-relationships">
            {relationships.map((edge, i) => {
              const outgoing = edge.source === node.id;
              const other = graph.nodes.find((item) => item.id === (outgoing ? edge.target : edge.source));
              return (
                <li className="map-relationship" key={`${edge.source}:${edge.target}:${i}`}>
                  <div><span>{outgoing ? "Imports" : "Imported by"} </span>{other?.detail.path ? <button className="link" onClick={() => onFocus(other.detail.path!)}>{other.label}</button> : <strong>{other?.label ?? (outgoing ? edge.target : edge.source)}</strong>}</div>
                  <span className={`dependency-badge${edge.confidence ? ` is-${edge.confidence}` : ""}`}>{edge.confidence === "verified" ? "Verified" : edge.confidence === "inferred" ? "Inferred" : "Unclassified"}</span>
                  {(edge.count ?? 1) > 1 && <span className="dependency-count">{number.format(edge.count!)} imports</span>}
                  {edge.evidence && edge.evidence.length > 0 && (
                    <ul className="map-evidence">{edge.evidence.map((evidence, index) => <li key={`${evidence.file}:${evidence.line}:${index}`}>
                      <button className="link" onClick={() => onFocus(evidence.file, evidence.line)} title={`Open ${evidence.file} at line ${evidence.line}`}>{evidence.file}:{evidence.line}</button>
                      <code>{evidence.text}</code>
                    </li>)}</ul>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>
      {readable && (
        <section className="detail-section">
          <div className="source-head"><h3>Source preview{preview.line !== undefined && <span className="source-citation">Cited line {preview.line}</span>}</h3><code title={path}>{path} · {preview.line === undefined ? "first 60 lines" : `lines ${preview.start}–${preview.end}`}</code></div>
          {status === "pending" && <p className="source-status" role="status">Loading source…</p>}
          {status === "error" && <p className="source-status is-error" role="status">Source could not be loaded. <button className="link" onClick={() => setAttempt((value) => value + 1)}>Retry</button></p>}
          {source && source.lines.length === 0 && <p className="source-status">No source lines available.</p>}
          {source && source.lines.length > 0 && <MapSourcePreview source={source} sourceLine={preview.line} />}
        </section>
      )}
    </section>
  );
}
