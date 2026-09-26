import { useState } from "react";
import type { Entry, ProjectInfo } from "./types";

const KIND_TEXT: Record<string, string> = {
  compose: "compose",
  docker: "docker",
  procfile: "procfile",
  pyproject: "script",
  shell: "shell",
  "package-main": "python -m",
  "main-guard": "__main__",
};

const number = new Intl.NumberFormat();

interface Props {
  project: ProjectInfo;
  current: string | null;
  onOpen: (entry: Entry) => void;
}

export function Sidebar({ project, current, onOpen }: Props) {
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const entries = project.entries.filter((e) => !needle || e.label.toLowerCase().includes(needle) || e.target.toLowerCase().includes(needle));
  const { stats } = project;
  return (
    <aside className="sidebar">
      <header className="sidebar-head">
        <div className="brand">flowmap</div>
        <h1 title={project.root}>{project.name}</h1>
        <p className="stats">
          {number.format(stats.files)} files · {number.format(stats.functions)} functions
        </p>
      </header>
      <div className="section-title">
        Entry points <span className="count">{project.entries.length}</span>
      </div>
      <input
        className="filter"
        type="search"
        placeholder="Filter entry points"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      <ul className="entries">
        {entries.map((entry) => (
          <li key={entry.target}>
            <button
              className={entry.target === current ? "entry is-current" : "entry"}
              onClick={() => onOpen(entry)}
              title={entry.sources.map((s) => s.text).join("\n")}
            >
              <code className="entry-label">{entry.label}</code>
              <span className="entry-meta">
                <span className={`kind kind-${entry.kind}`}>{KIND_TEXT[entry.kind] ?? entry.kind}</span>
                <span>{number.format(entry.reach)} functions</span>
              </span>
            </button>
          </li>
        ))}
        {entries.length === 0 && <li className="none">No entry point matches.</li>}
      </ul>
    </aside>
  );
}
