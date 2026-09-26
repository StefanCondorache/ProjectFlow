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
  onClose: () => void;
}

interface Where {
  file: string;
  from: number;
  focus?: number;
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
            {v} →
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

export function Details({ node, following, onToggle, onEnter, onFollow, onClose }: Props) {
  const d = node.detail;
  const where = sourceFor(node);
  const [source, setSource] = useState<SourceLines | null>(null);

  useEffect(() => {
    setSource(null);
    if (!where) return;
    let live = true;
    api
      .source(where.file, where.from, where.from + (where.focus ? 12 : 40))
      .then((s) => live && setSource(s))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [where?.file, where?.from, where?.focus]);

  const isCall = node.kind === "step" || node.kind === "group";
  return (
    <div className="details">
      <header className="details-head">
        <span className="details-kind">{KIND_NAME[node.kind] ?? node.kind}</span>
        <button className="close" onClick={onClose} aria-label="Close details" title="Close (Esc)">
          ×
        </button>
      </header>
      <h2 className="details-title">{node.label}</h2>
      {d.doc && <p className="details-doc">{d.doc}</p>}
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
            <dd>
              <code>{d.text}</code>
            </dd>
          </>
        )}
        {d.params && d.params.length > 0 && (
          <>
            <dt>Takes</dt>
            <dd>
              <Chips values={d.params} onFollow={(v) => onFollow(node.id, v)} />
            </dd>
          </>
        )}
        {d.receives && d.receives.length > 0 && (
          <>
            <dt>Receives it as</dt>
            <dd>
              <Chips values={d.receives} />
            </dd>
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
            <dd>
              <Chips values={d.args} />
            </dd>
          </>
        )}
        {d.defs && d.defs.length > 0 && (
          <>
            <dt>Data out</dt>
            <dd>
              <Chips values={d.defs} onFollow={(v) => onFollow(node.id, v)} />
            </dd>
          </>
        )}
        {d.io && (
          <>
            <dt>Moves data</dt>
            <dd>
              {d.io[0]} · {IO_TEXT[d.io[1]] ?? d.io[1]}
              {d.external && (
                <>
                  {" "}
                  via <code>{d.external}</code>
                </>
              )}
            </dd>
          </>
        )}
        {d.confidence && (
          <>
            <dt>Resolved</dt>
            <dd className={`confidence confidence-${d.confidence}`}>{CONFIDENCE[d.confidence] ?? d.confidence}</dd>
          </>
        )}
      </dl>
      <div className="actions">
        {node.kind === "step" && d.expandable && !following && <button onClick={() => onToggle(node)}>Open here</button>}
        {node.kind === "group" && !following && <button onClick={() => onToggle(node)}>Close</button>}
        {d.target && <button onClick={() => onEnter(d.target!)}>Show on its own</button>}
      </div>
      {source && (
        <pre className="source">
          {source.lines.map((line, i) => {
            const number = source.start + i;
            return (
              <div key={number} className={number === where?.focus ? "line is-focus" : "line"}>
                <span className="ln">{number}</span>
                {line || " "}
              </div>
            );
          })}
        </pre>
      )}
    </div>
  );
}
