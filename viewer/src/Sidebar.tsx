import { useEffect, useState } from "react";
import { api, type SearchHit } from "./api";
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

const GROUPS = [
  { title: "Deployed", kinds: ["compose", "docker", "procfile"] },
  { title: "Package scripts", kinds: ["pyproject"] },
  { title: "Shell scripts", kinds: ["shell"] },
  { title: "Runnable packages", kinds: ["package-main"] },
  { title: "Scripts", kinds: ["main-guard"] },
];

const number = new Intl.NumberFormat();

interface Props {
  project: ProjectInfo;
  current: string | null;
  onOpen: (entry: Entry) => void;
  onFunction: (id: string) => void;
}

export function Sidebar({ project, current, onOpen, onFunction }: Props) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const needle = query.trim().toLowerCase();

  useEffect(() => {
    if (!needle) {
      setHits([]);
      return;
    }
    const timer = window.setTimeout(() => {
      api
        .search(needle)
        .then(setHits)
        .catch(() => setHits([]));
    }, 150);
    return () => window.clearTimeout(timer);
  }, [needle]);

  const matches = (e: Entry) => !needle || e.label.toLowerCase().includes(needle) || e.target.toLowerCase().includes(needle);
  const { stats } = project;
  const groups = GROUPS.map((g) => ({ ...g, entries: project.entries.filter((e) => g.kinds.includes(e.kind) && matches(e)) })).filter(
    (g) => g.entries.length > 0,
  );

  return (
    <aside className="sidebar">
      <header className="sidebar-head">
        <div className="brand">flowmap</div>
        <h1 title={project.root}>{project.name}</h1>
        <p className="stats">
          {number.format(stats.files)} files · {number.format(stats.functions)} functions · {project.entries.length} entry points
        </p>
      </header>
      <input
        className="filter"
        type="search"
        placeholder="Search entry points and functions"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        aria-label="Search entry points and functions"
      />
      <div className="sidebar-list">
        {groups.map((group) => (
          <section key={group.title}>
            <div className="section-title">
              {group.title} <span className="count">{group.entries.length}</span>
            </div>
            <ul className="entries">
              {group.entries.map((entry) => (
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
            </ul>
          </section>
        ))}
        {needle && (
          <section>
            <div className="section-title">
              Functions <span className="count">{hits.length === 30 ? "30+" : hits.length}</span>
            </div>
            <ul className="entries">
              {hits.map((hit) => (
                <li key={hit.id}>
                  <button className={hit.id === current ? "entry is-current" : "entry"} onClick={() => onFunction(hit.id)} title={hit.doc ?? ""}>
                    <code className="entry-label">{hit.label}</code>
                    <span className="entry-meta">
                      <span>
                        {hit.file}:{hit.line}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
        {needle && groups.length === 0 && hits.length === 0 && <p className="none">Nothing matches “{query}”.</p>}
      </div>
    </aside>
  );
}
