import { useState } from "react";
import type { SimChanges, SimValue, Simulation } from "./sim";

const SPEEDS = [0.5, 1, 2, 4];

interface Props {
  sim: Simulation;
  index: number;
  playing: boolean;
  speed: number;
  busy: boolean;
  onPlay: () => void;
  onPause: () => void;
  onStep: (delta: number) => void;
  onRestart: () => void;
  onSpeed: (speed: number) => void;
  onChoose: (value: string) => void;
}

const cx = (...names: (string | false | null | undefined)[]) => names.filter(Boolean).join(" ");

export function SimPanel({ sim, index, playing, speed, busy, onPlay, onPause, onStep, onRestart, onSpeed, onChoose }: Props) {
  const frame = sim.frames[index];
  const atEnd = index >= sim.frames.length - 1;
  if (!frame) return <div className="sim-panel">Nothing to simulate here.</div>;
  const scope = frame.stack[frame.stack.length - 1];
  const outer = frame.stack.slice(0, -1).reverse();
  const names = Object.keys(scope.vars);

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
        <span className="sim-progress">
          step {index + 1} of {sim.frames.length}
          {sim.status === "choose" && "+"}
        </span>
      </div>
      <progress className="sim-bar" max={Math.max(1, sim.frames.length - 1)} value={index} />

      <p className="sim-note">{frame.note}</p>

      {atEnd && sim.status === "choose" && sim.choice && (
        <div className="sim-choice" role="group" aria-label="Choose a way">
          <div className="sim-choice-question">
            <code>{sim.choice.question}</code>
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
      {atEnd && sim.status === "raised" && <p className="sim-end sim-end-error">⚠ Stopped: this path raises an error.</p>}
      {atEnd && sim.status === "limit" && <p className="sim-end">Stopped after {sim.frames.length} steps.</p>}

      <div className="sim-scope">
        <div className="sim-where">
          {[...frame.stack].map((s, i) => (
            <span key={i} className={i === frame.stack.length - 1 ? "is-here" : ""}>
              {i > 0 && " › "}
              {s.label}
            </span>
          ))}
        </div>
        {frame.changes.removed.length > 0 && (
          <div className="sim-removed">no longer here: {frame.changes.removed.join(", ")}</div>
        )}
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
            <OuterScope key={i} label={s.label} vars={s.vars} />
          ))}
        </div>
      )}
    </div>
  );
}

function OuterScope({ label, vars }: { label: string; vars: Record<string, SimValue> }) {
  const [open, setOpen] = useState(false);
  const names = Object.keys(vars);
  return (
    <div className="sim-outer-scope">
      <button className="sim-outer-head" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {open ? "▾" : "▸"} waiting in <strong>{label}</strong> · {names.length} value{names.length === 1 ? "" : "s"}
      </button>
      {open && (
        <div className="tree">
          {names.map((name) => (
            <ValueRow key={name} name={name} path={name} value={vars[name]} changes={NO_CHANGES} depth={0} />
          ))}
        </div>
      )}
    </div>
  );
}

const NO_CHANGES: SimChanges = { added: [], changed: [], removed: [] };

function Literal({ value }: { value: string | number | boolean | null }) {
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

function ValueRow({
  name,
  path,
  value,
  changes,
  depth,
}: {
  name: string;
  path: string;
  value: SimValue;
  changes: SimChanges;
  depth: number;
}) {
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
        {key}: <Literal value={value.v} /> {badge}
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
