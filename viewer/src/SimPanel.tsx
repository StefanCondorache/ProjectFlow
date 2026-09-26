import { useState, type ReactNode } from "react";
import { readInput, type SimChanges, type SimOutput, type SimValue, type Simulation } from "./sim";
import type { FlowNode } from "./types";

const SPEEDS = [0.5, 1, 2, 4];

interface Props {
  sim: Simulation;
  index: number;
  /** The diagram box the current frame is at. */
  node: FlowNode | null;
  playing: boolean;
  speed: number;
  busy: boolean;
  onPlay: () => void;
  onPause: () => void;
  onStep: (delta: number) => void;
  onRestart: () => void;
  onSpeed: (speed: number) => void;
  onChoose: (value: string) => void;
  /** Change the data the run starts with. */
  onSetup: () => void;
  /** Open the current (closed) step so the data goes through its inside. */
  onGoInside: (node: FlowNode) => void;
  /** Use ``value`` as the result of the call at ``nodeId``. */
  onProvide: (nodeId: string, value: unknown) => void;
}

const cx = (...names: (string | false | null | undefined)[]) => names.filter(Boolean).join(" ");

export function SimPanel(props: Props) {
  const { sim, index, node, playing, speed, busy, onPlay, onPause, onStep, onRestart, onSpeed, onChoose, onSetup, onGoInside, onProvide } = props;
  const frame = sim.frames[index];
  const atEnd = index >= sim.frames.length - 1;
  if (!frame) {
    return (
      <div className="sim-panel">
        <p className="sim-end">{sim.status === "unreached" ? "The walk never reached this starting point." : "Nothing to simulate here."}</p>
        <button className="sim-setup-link" onClick={onSetup}>
          ⚙ Change the data
        </button>
      </div>
    );
  }
  const scope = frame.stack[frame.stack.length - 1];
  const outer = frame.stack.slice(0, -1).reverse();
  const names = Object.keys(scope.vars);
  const written = sim.frames.slice(0, index + 1).flatMap((f) => f.outputs);
  const canGoInside = node?.kind === "step" && Boolean(node.detail.target) && !node.detail.recursive;
  const results = (node?.kind === "step" || node?.kind === "io") && node.detail.defs?.length === 1 ? node.detail.defs : [];
  const unknownResult = results.find((name) => scope.vars[name]?.t === "?");

  return (
    <div className="sim-panel">
      <div className="sim-controls">
        <button onClick={onRestart} title="Back to the start" aria-label="Restart">
          ⏮
        </button>
        <button onClick={() => onStep(-1)} disabled={index === 0} title="Previous step" aria-label="Previous step">
          ◀
        </button>
        {playing ? (
          <button className="sim-play" onClick={onPause} title="Pause" aria-label="Pause">
            ⏸
          </button>
        ) : (
          <button className="sim-play" onClick={onPlay} disabled={atEnd} title="Play" aria-label="Play">
            ▶
          </button>
        )}
        <button onClick={() => onStep(1)} disabled={atEnd} title="Next step" aria-label="Next step">
          ▶|
        </button>
        <label className="sim-speed" title="Speed">
          <select value={speed} onChange={(e) => onSpeed(Number(e.target.value))}>
            {SPEEDS.map((s) => (
              <option key={s} value={s}>
                {s}×
              </option>
            ))}
          </select>
        </label>
        <button onClick={onSetup} title="Change the data the run starts with" aria-label="Change the data">
          ⚙ Data
        </button>
        <span className="sim-progress">
          step {index + 1} of {sim.frames.length}
          {sim.status === "choose" && "+"}
        </span>
      </div>
      <progress className="sim-bar" max={Math.max(1, sim.frames.length - 1)} value={index} />

      <p className={cx("sim-note", frame.error && "sim-note-error")}>{frame.note}</p>

      {frame.error && (
        <div className="sim-error" role="alert">
          <div>
            <span aria-hidden>⚠ </span>
            <strong>{frame.error.type}</strong>
            {frame.error.message && <>: {frame.error.message}</>}
          </div>
          <div className="sim-error-where">
            raised at <code>{frame.error.file}:{frame.error.line}</code>
          </div>
        </div>
      )}

      {canGoInside && node && (
        <button className="sim-inside" onClick={() => onGoInside(node)} disabled={busy} title="Open this step and send the data through it">
          ⤵ Go inside <code>{node.label}</code>
        </button>
      )}

      {unknownResult && node && <Provide name={unknownResult} busy={busy} onUse={(value) => onProvide(node.id, value)} />}

      {frame.outputs.length > 0 && (
        <div className="sim-outputs">
          {frame.outputs.map((output, i) => (
            <OutputRow key={i} output={output} />
          ))}
        </div>
      )}

      {atEnd && sim.status === "choose" && sim.choice && (
        <div className="sim-choice" role="group" aria-label="Choose a way">
          <div className="sim-choice-question">
            <code>{sim.choice.question}</code>
            {sim.choice.node.includes("@") && <span className="sim-round"> · round {sim.choice.node.split("@").slice(-1)[0]}</span>}
          </div>
          <p>This depends on data only a real run would have. Which way should the data go?</p>
          <div className="sim-choice-options">
            {sim.choice.options.map((option) => (
              <button key={option.value} onClick={() => onChoose(option.value)} disabled={busy}>
                {option.label}
              </button>
            ))}
          </div>
        </div>
      )}
      {atEnd && sim.status === "done" && <p className="sim-end">✓ Reached the end.</p>}
      {atEnd && sim.status === "raised" && sim.error && (
        <p className="sim-end sim-end-error">
          ⚠ Stopped: {sim.error.type} was not caught ({sim.error.file}:{sim.error.line}).
        </p>
      )}
      {atEnd && sim.status === "limit" && <p className="sim-end">Stopped after {sim.frames.length} steps: too long to follow.</p>}
      {atEnd && sim.status === "unreached" && <p className="sim-end">The walk never reached the starting point.</p>}

      <div className="sim-scope">
        <div className="sim-where">
          {[...frame.stack].map((s, i) => (
            <span key={i} className={i === frame.stack.length - 1 ? "is-here" : ""}>
              {i > 0 && " › "}
              {s.label}
            </span>
          ))}
        </div>
        {frame.changes.removed.length > 0 && <div className="sim-removed">no longer here: {frame.changes.removed.join(", ")}</div>}
        {names.length === 0 ? (
          <p className="sim-empty">No data in this function yet.</p>
        ) : (
          <div className="tree" role="tree">
            {names.map((name) => (
              <ValueRow key={name} name={name} path={name} value={scope.vars[name]} changes={frame.changes} depth={0} />
            ))}
          </div>
        )}
      </div>

      {outer.length > 0 && (
        <div className="sim-outer">
          {outer.map((s, i) => (
            <Collapsible key={i} head={
              <>
                waiting in <strong>{s.label}</strong> · {Object.keys(s.vars).length} value{Object.keys(s.vars).length === 1 ? "" : "s"}
              </>
            }>
              <div className="tree">
                {Object.keys(s.vars).map((name) => (
                  <ValueRow key={name} name={name} path={name} value={s.vars[name]} changes={NO_CHANGES} depth={0} />
                ))}
              </div>
            </Collapsible>
          ))}
        </div>
      )}

      {written.length > 0 && (
        <Collapsible
          head={
            <>
              written so far · {written.length} output{written.length === 1 ? "" : "s"} (recorded, never really written)
            </>
          }
        >
          <div className="sim-outputs">
            {written.map((output, i) => (
              <OutputRow key={i} output={output} />
            ))}
          </div>
        </Collapsible>
      )}
    </div>
  );
}

function Collapsible({ head, children }: { head: ReactNode; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="sim-outer-scope">
      <button className="sim-outer-head" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {open ? "▾" : "▸"} {head}
      </button>
      {open && children}
    </div>
  );
}

function Provide({ name, busy, onUse }: { name: string; busy: boolean; onUse: (value: unknown) => void }) {
  const [text, setText] = useState<string | null>(null);
  if (text === null) {
    return (
      <button className="link sim-provide-open" onClick={() => setText("")}>
        Give <code>{name}</code> a value
      </button>
    );
  }
  return (
    <form
      className="sim-provide"
      onSubmit={(event) => {
        event.preventDefault();
        if (text.trim()) onUse(readInput(text).value);
      }}
    >
      <label htmlFor="provide">
        <code>{name}</code> is really (JSON, or plain text):
      </label>
      <textarea id="provide" value={text} rows={3} spellCheck={false} autoFocus onChange={(e) => setText(e.target.value)} />
      <div className="run-actions">
        <button type="submit" className="primary" disabled={busy || !text.trim()}>
          Use it
        </button>
        <button type="button" onClick={() => setText(null)}>
          Cancel
        </button>
      </div>
    </form>
  );
}

const HOW: Record<string, string> = { print: "prints", write: "writes", writelines: "writes", "json.dump": "saves JSON to" };

function OutputRow({ output }: { output: SimOutput }) {
  const how = HOW[output.how] ?? output.how;
  const text = output.data.t === "val" && typeof output.data.v === "string" ? output.data.v : null;
  const toConsole = output.target === "console" || output.target === "log";
  return (
    <div className="sim-output">
      <div className="sim-output-head">
        <span className="sim-output-how">{toConsole ? (output.target === "log" ? `log · ${output.how}` : how) : how}</span>
        {!toConsole && <code className="sim-output-target">{output.target}</code>}
      </div>
      {text !== null ? (
        <pre className="sim-output-text">{text}</pre>
      ) : (
        <div className="tree">
          <ValueRow name="data" path="data" value={output.data} changes={NO_CHANGES} depth={0} />
        </div>
      )}
    </div>
  );
}

const NO_CHANGES: SimChanges = { added: [], changed: [], removed: [] };

function Literal({ value, type }: { value: string | number | boolean | null; type?: string }) {
  if (type) {
    return (
      <span className="lit lit-typed">
        {String(value)} <span className="lit-type">{type}</span>
      </span>
    );
  }
  if (typeof value === "string") return <span className="lit lit-str">{JSON.stringify(value)}</span>;
  if (typeof value === "number") return <span className="lit lit-num">{value}</span>;
  return <span className="lit lit-const">{String(value)}</span>;
}

function children(value: SimValue): [string, SimValue][] {
  if (value.t === "dict" || value.t === "obj") return Object.entries(value.v);
  if (value.t === "list") return value.v.map((v, i) => [String(i), v]);
  if (value.t === "?" && value.v) return Object.entries(value.v);
  return [];
}

function opening(value: SimValue): [string, string] {
  if (value.t === "dict") return ["{", "}"];
  if (value.t === "obj") return [`${value.cls} {`, "}"];
  if (value.t === "list") return value.kind === "tuple" ? ["(", ")"] : value.kind === "set" ? ["{", "}"] : ["[", "]"];
  return ["", ""];
}

function ValueRow({ name, path, value, changes, depth }: { name: string; path: string; value: SimValue; changes: SimChanges; depth: number }) {
  const [collapsed, setCollapsed] = useState(depth >= 2);
  const added = changes.added.includes(path);
  const changed = changes.changed.includes(path);
  const kids = children(value);
  const badge = added ? <span className="badge badge-added">new</span> : changed ? <span className="badge badge-changed">changed</span> : null;
  const rowClass = cx("tree-row", added && "is-added", changed && "is-changed");
  const key = <span className="tree-key">{name}</span>;

  if (value.t === "val") {
    return (
      <div className={rowClass} role="treeitem" style={{ paddingLeft: depth * 14 }}>
        {key}: <Literal value={value.v} type={value.type} /> {badge}
      </div>
    );
  }
  if (value.t === "ref") {
    return (
      <div className={rowClass} role="treeitem" style={{ paddingLeft: depth * 14 }}>
        {key}: <span className="ref">{value.v}</span> {badge}
      </div>
    );
  }
  if (value.t === "?" && kids.length === 0) {
    return (
      <div className={rowClass} role="treeitem" style={{ paddingLeft: depth * 14 }} title="Only a real run would know this value">
        {key}: <span className="unknown">‹{value.from}›</span> {badge}
      </div>
    );
  }
  const [open, close] = opening(value);
  const more = "more" in value && value.more ? value.more : 0;
  return (
    <div role="treeitem" aria-expanded={!collapsed}>
      <div className={rowClass} style={{ paddingLeft: depth * 14 }}>
        <button className="tree-toggle" onClick={() => setCollapsed((c) => !c)} aria-label={collapsed ? "Expand" : "Collapse"}>
          {collapsed ? "▸" : "▾"}
        </button>
        {key}:{" "}
        {value.t === "?" ? (
          <span className="unknown">‹{value.from}›</span>
        ) : (
          <span className="tree-brace">
            {open}
            {collapsed && `${kids.length} ${close}`}
          </span>
        )}{" "}
        {badge}
      </div>
      {!collapsed && (
        <>
          {kids.map(([k, v]) => (
            <ValueRow key={k} name={k} path={`${path}.${k}`} value={v} changes={changes} depth={depth + 1} />
          ))}
          {more > 0 && (
            <div className="tree-row tree-more" style={{ paddingLeft: (depth + 1) * 14 }}>
              … {more} more
            </div>
          )}
          {value.t !== "?" && (
            <div className="tree-row tree-brace" style={{ paddingLeft: depth * 14 + 16 }}>
              {close}
            </div>
          )}
        </>
      )}
    </div>
  );
}
