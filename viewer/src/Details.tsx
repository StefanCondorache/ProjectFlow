import { useEffect, useState } from "react";
import { api } from "./api";
import type { FlowNode, SourceLines } from "./types";

const KIND_NAME: Record<string, string> = {
  use: "Uses the data",
  start: "Start",
  end: "End",
  step: "Step",
  io: "Data in / out",
  decision: "Decision",
  loop: "Loop",
  try: "Try block",
  handler: "Error handler",
  raise: "Raises an error",
  group: "Opened step",
  file: "File",
};

const CONFIDENCE: Record<string, string> = {
  exact: "exact: followed names and imports",
  inferred: "inferred from types (annotations, constructors, return values)",
  guess: "guessed: the variable is named like a project class",
};

const IO_TEXT: Record<string, string> = { in: "into the program", out: "out of the program", inout: "in and out" };

interface Props {
  node: FlowNode;
  following: boolean;
  onToggle: (node: FlowNode) => void;
  onEnter: (functionId: string) => void;
  onFollow: (at: string, variable: string) => void;
  /** Start a simulation at this node (absent: not offered). */
  onSimulateFrom?: (node: FlowNode) => void;
  onClose: () => void;
}

// Boxes a simulation can start at: those the walk passes through.
const STARTS = new Set(["start", "step", "io", "decision", "loop", "try", "handler", "raise", "group"]);

interface Where {
  file: string;
  from: number;
  focus?: number;
}

export function requestNodeSource(where: Where, onResult: (source: SourceLines) => void, onError: () => void): () => void {
  let live = true;
  api.source(where.file, where.from, where.from + (where.focus ? 12 : 40)).then(
    (source) => live && onResult(source),
    () => live && onError(),
  );
  return () => {
    live = false;
  };
}

interface SourceState {
  location: string;
  status: "pending" | "ready" | "error";
  source: SourceLines | null;
}

function sourceFor(node: FlowNode): Where | null {
  const d = node.detail;
  if ((node.kind === "step" || node.kind === "group") && d.target && d.file && d.def_line) return { file: d.file, from: d.def_line };
  if (node.kind === "start" && d.file && d.line) return { file: d.file, from: d.line };
  if (d.at && d.line) return { file: d.at, from: Math.max(1, d.line - 4), focus: d.line };
  return null;
}

function Chips({ values, onFollow }: { values: string[]; onFollow?: (value: string) => void }) {
  return (
    <>
      {values.map((v, i) =>
        onFollow ? (
          <button key={i} className="chip chip-follow" onClick={() => onFollow(v)} title={`Follow ${v} through the code`}>
            {v} <span aria-hidden="true">→</span>
          </button>
        ) : (
          <code key={i} className="chip">
            {v}
          </code>
        ),
      )}
    </>
  );
}

export function Details({ node, following, onToggle, onEnter, onFollow, onSimulateFrom, onClose }: Props) {
  const d = node.detail;
  const where = sourceFor(node);
  const sourceKey = where ? JSON.stringify([where.file, where.from, where.focus]) : "";
  const [sourceState, setSourceState] = useState<SourceState>({ location: "", status: "pending", source: null });
  const [sourceAttempt, setSourceAttempt] = useState(0);
  const source = sourceState.location === sourceKey ? sourceState.source : null;
  const sourceStatus = sourceState.location === sourceKey ? sourceState.status : "pending";

  useEffect(() => {
    setSourceState({ location: sourceKey, status: "pending", source: null });
    if (!where) return;
    return requestNodeSource(
      where,
      (loaded) => setSourceState({ location: sourceKey, status: "ready", source: loaded }),
      () => setSourceState({ location: sourceKey, status: "error", source: null }),
    );
  }, [sourceKey, sourceAttempt]);

  const isCall = node.kind === "step" || node.kind === "group";
  const hasContext = Boolean(
    (isCall && d.file) ||
    (d.at && d.line && node.kind !== "start") ||
    (node.kind === "start" && d.file && d.line) ||
    (d.text && d.text !== node.label) ||
    d.confidence,
  );
  const hasData = Boolean(d.params?.length || d.receives?.length || d.returns !== undefined || d.args?.length || d.defs?.length || d.io);
  const hasActions = Boolean(
    (onSimulateFrom && STARTS.has(node.kind)) ||
    (node.kind === "step" && d.expandable && !following) ||
    (node.kind === "group" && !following) ||
    d.target,
  );
  return (
    <section className="details" aria-label="Selected node details">
      <header className="details-head">
        <div>
          <span className="details-eyebrow">Node inspector</span>
          <span className={`details-kind details-kind-${node.kind}`}>{KIND_NAME[node.kind] ?? node.kind}</span>
        </div>
        <button className="close" onClick={onClose} aria-label="Close details" title="Close (Esc)">
          ×
        </button>
      </header>
      <h2 className="details-title">{node.label}</h2>
      {d.doc && <p className="details-doc">{d.doc}</p>}
      {hasContext && (
        <section className="detail-section">
          <h3>Context</h3>
          <dl className="facts">
            {isCall && d.file && (
              <>
                <dt>Defined in</dt>
                <dd>
                  <code>
                    {d.file}
                    {d.def_line ? `:${d.def_line}` : ""}
                  </code>
                </dd>
              </>
            )}
            {node.kind === "start" && d.file && d.line && (
              <>
                <dt>Starts at</dt>
                <dd><code>{d.file}:{d.line}</code></dd>
              </>
            )}
            {d.at && d.line && node.kind !== "start" && (
              <>
                <dt>{isCall ? "Called at" : "At"}</dt>
                <dd>
                  <code>
                    {d.at}:{d.line}
                  </code>
                </dd>
              </>
            )}
            {d.text && d.text !== node.label && (
              <>
                <dt>Code</dt>
                <dd><code>{d.text}</code></dd>
              </>
            )}
            {d.confidence && (
              <>
                <dt>Resolved</dt>
                <dd className={`confidence confidence-${d.confidence}`}>{CONFIDENCE[d.confidence] ?? d.confidence}</dd>
              </>
            )}
          </dl>
        </section>
      )}
      {hasData && (
        <section className="detail-section">
          <h3>Data flow</h3>
          <dl className="facts">
            {d.params && d.params.length > 0 && (
              <>
                <dt>Takes</dt>
                <dd><Chips values={d.params} onFollow={(v) => onFollow(node.id, v)} /></dd>
              </>
            )}
            {d.receives && d.receives.length > 0 && (
              <>
                <dt>Receives it as</dt>
                <dd><Chips values={d.receives} /></dd>
              </>
            )}
            {d.returns !== undefined && (
              <>
                <dt>Returns it</dt>
                <dd>{d.returns ? "yes, back to its caller" : "no"}</dd>
              </>
            )}
            {d.args && d.args.length > 0 && (
              <>
                <dt>Data in</dt>
                <dd><Chips values={d.args} /></dd>
              </>
            )}
            {d.defs && d.defs.length > 0 && (
              <>
                <dt>Data out</dt>
                <dd><Chips values={d.defs} onFollow={(v) => onFollow(node.id, v)} /></dd>
              </>
            )}
            {d.io && (
              <>
                <dt>Moves data</dt>
                <dd>
                  {d.io[0]} · {IO_TEXT[d.io[1]] ?? d.io[1]}
                  {d.external && <> via <code>{d.external}</code></>}
                </dd>
              </>
            )}
          </dl>
        </section>
      )}
      {hasActions && (
        <section className="detail-section">
          <h3>Explore</h3>
          <div className="actions">
            {onSimulateFrom && STARTS.has(node.kind) && (
              <button className="primary" onClick={() => onSimulateFrom(node)} title="Send data through the diagram from this point on">
                <span aria-hidden="true">▶</span> Simulate from here
              </button>
            )}
            {node.kind === "step" && d.expandable && !following && <button onClick={() => onToggle(node)}>Open here</button>}
            {node.kind === "group" && !following && <button onClick={() => onToggle(node)}>Close</button>}
            {d.target && <button onClick={() => onEnter(d.target!)}>Show on its own</button>}
          </div>
        </section>
      )}
      {where && (
        <section className="detail-section">
          <div className="source-head">
            <h3>Source</h3>
            <code title={where.file}>{where.file}:{where.focus ?? where.from}</code>
          </div>
          {sourceStatus === "pending" && <p className="source-status" role="status">Loading source…</p>}
          {sourceStatus === "error" && (
            <p className="source-status is-error" role="status">
              Source could not be loaded. <button className="link" onClick={() => setSourceAttempt((attempt) => attempt + 1)}>Retry</button>
            </p>
          )}
          {source && source.lines.length === 0 && <p className="source-status">No source lines available.</p>}
          {source && source.lines.length > 0 && (
            <pre className="source" aria-label={`Source from ${source.file}`}>
              <code>
                {source.lines.map((line, i) => {
                  const number = source.start + i;
                  return (
                    <span key={number} className={number === where.focus ? "line is-focus" : "line"}>
                      <span className="ln">{number}</span>
                      {line || " "}
                    </span>
                  );
                })}
              </code>
            </pre>
          )}
        </section>
      )}
    </section>
  );
}
