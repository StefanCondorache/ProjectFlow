import { useEffect, useState } from "react";
import { joinArgs, plainOf, readInput, splitArgs, type Expects, type SimSetup } from "./sim";

interface Props {
  /** Where the run starts, as the diagram names it. */
  label: string;
  /** What there is at that point; null while it loads. */
  expects: Expects | null;
  setup: SimSetup;
  busy: boolean;
  onRun: (setup: SimSetup) => void;
  onCancel: () => void;
}

const pretty = (value: unknown) => JSON.stringify(value, null, 2);
const short = (value: unknown) => {
  const text = JSON.stringify(value);
  return text.length > 60 ? `${text.slice(0, 59)}…` : text;
};

/** The data a run starts with: the variables at its starting point (with an
 * example of what each takes), the command line and environment variables
 * the code reads, and whether to go inside every step. */
export function RunSetup({ label, expects, setup, busy, onRun, onCancel }: Props) {
  const [inputs, setInputs] = useState<Record<string, string>>(() =>
    Object.fromEntries(Object.entries(setup.inputs).map(([k, v]) => [k, typeof v === "string" ? v : pretty(v)])),
  );
  const [argText, setArgText] = useState(setup.argv ? joinArgs(setup.argv) : "");
  const [argGiven, setArgGiven] = useState(setup.argv !== null);
  const [env, setEnv] = useState<Record<string, string>>(() =>
    Object.fromEntries(Object.entries(setup.env).filter(([, v]) => v !== null) as [string, string][]),
  );
  const [unset, setUnset] = useState<Set<string>>(() => new Set(Object.entries(setup.env).filter(([, v]) => v === null).map(([k]) => k)));
  const [provided, setProvided] = useState(setup.provided);
  const [autoOpen, setAutoOpen] = useState(setup.autoOpen);

  // A new starting point brings new variables: keep what still applies.
  useEffect(() => {
    if (!expects) return;
    setInputs((old) => Object.fromEntries(Object.entries(old).filter(([k]) => k in expects.vars)));
  }, [expects]);

  const run = () => {
    const given: Record<string, unknown> = {};
    for (const [name, text] of Object.entries(inputs)) if (text.trim()) given[name] = readInput(text).value;
    const environment: Record<string, string | null> = {};
    for (const [name, value] of Object.entries(env)) if (value !== "") environment[name] = value;
    for (const name of unset) environment[name] = null;
    onRun({ at: setup.at, inputs: given, env: environment, argv: argGiven ? splitArgs(argText) : null, provided, autoOpen });
  };

  const vars = expects ? Object.entries(expects.vars) : [];
  const envNames = expects ? [...new Set([...expects.env, ...Object.keys(env), ...unset])] : [];
  const providedIds = Object.keys(provided);

  return (
    <form
      className="run-setup"
      onSubmit={(event) => {
        event.preventDefault();
        run();
      }}
    >
      <h2 className="run-title">
        {setup.at ? "Run from" : "Run"} <code>{label}</code>
      </h2>
      {!expects && <p className="run-note">Looking at what the code has there…</p>}
      {expects && !expects.reached && (
        <p className="run-note run-warn">The walk does not get to this point on its own; the values below are used from where it stops.</p>
      )}

      {vars.length > 0 && (
        <section className="run-section">
          <h3>Data {setup.at ? "at this point" : "it starts with"}</h3>
          <p className="run-note">
            Fill in what you know; the rest stays as the code has it, or unknown (<span className="unknown">‹…›</span>). JSON, or plain text.
          </p>
          {vars.map(([name, info]) => {
            const text = inputs[name] ?? "";
            const typed = text.trim() ? readInput(text) : null;
            const known = info.known ? plainOf(info.value) : undefined;
            return (
              <div className="run-var" key={name}>
                <div className="run-var-head">
                  <label htmlFor={`in-${name}`}>
                    <code className="run-var-name">{name}</code>
                  </label>
                  {info.type && <code className="run-var-type">{info.type}</code>}
                  {known !== undefined && <span className="run-var-known">= {short(known)}</span>}
                  <span className="spacer" />
                  {info.template !== null && info.template !== undefined && (
                    <button type="button" className="link" onClick={() => setInputs((o) => ({ ...o, [name]: pretty(info.template) }))}>
                      example
                    </button>
                  )}
                  {text && (
                    <button type="button" className="link" onClick={() => setInputs((o) => ({ ...o, [name]: "" }))}>
                      clear
                    </button>
                  )}
                </div>
                <textarea
                  id={`in-${name}`}
                  value={text}
                  rows={Math.min(8, Math.max(1, text.split("\n").length))}
                  spellCheck={false}
                  placeholder={known !== undefined ? `${short(known)}  (from the code)` : "only a real run would know"}
                  onChange={(e) => setInputs((o) => ({ ...o, [name]: e.target.value }))}
                />
                {typed?.text && <span className="run-hint">read as text</span>}
              </div>
            );
          })}
        </section>
      )}

      {expects?.argv && (
        <section className="run-section">
          <h3>Command line</h3>
          <input
            type="text"
            className="run-args"
            value={argText}
            spellCheck={false}
            placeholder="e.g. --region us --days 3"
            aria-label="Command-line arguments"
            onChange={(e) => {
              setArgText(e.target.value);
              setArgGiven(true);
            }}
          />
          <label className="run-check">
            <input type="checkbox" checked={argGiven} onChange={(e) => setArgGiven(e.target.checked)} /> use this command line (empty: no arguments)
          </label>
          <p className="run-note">The program's own argparse set-up reads it. Unused: the run asks wherever it matters.</p>
        </section>
      )}

      {envNames.length > 0 && (
        <section className="run-section">
          <h3>Environment variables</h3>
          <div className="run-env">
            {envNames.map((name) => (
              <div className="run-env-row" key={name}>
                <label htmlFor={`env-${name}`}>
                  <code>{name}</code>
                </label>
                <input
                  id={`env-${name}`}
                  type="text"
                  value={env[name] ?? ""}
                  disabled={unset.has(name)}
                  placeholder={unset.has(name) ? "not set" : "unknown"}
                  spellCheck={false}
                  onChange={(e) => setEnv((o) => ({ ...o, [name]: e.target.value }))}
                />
                <label className="run-check" title="The variable is not set at all">
                  <input
                    type="checkbox"
                    checked={unset.has(name)}
                    onChange={(e) =>
                      setUnset((old) => {
                        const next = new Set(old);
                        if (e.target.checked) next.add(name);
                        else next.delete(name);
                        return next;
                      })
                    }
                  />{" "}
                  unset
                </label>
              </div>
            ))}
          </div>
        </section>
      )}

      {providedIds.length > 0 && (
        <section className="run-section">
          <h3>Given results</h3>
          {providedIds.map((id) => (
            <div className="run-given" key={id}>
              <code>{id}</code> = <code>{short(provided[id])}</code>
              <button
                type="button"
                className="link"
                onClick={() => setProvided((o) => Object.fromEntries(Object.entries(o).filter(([k]) => k !== id)))}
              >
                remove
              </button>
            </div>
          ))}
        </section>
      )}

      <section className="run-section">
        <label className="run-check">
          <input type="checkbox" checked={autoOpen} onChange={(e) => setAutoOpen(e.target.checked)} /> Go inside every step the data enters
        </label>
      </section>

      <div className="run-actions">
        <button type="submit" className="primary" disabled={busy || !expects}>
          ▶ Run
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
