import { useEffect, useRef, useState } from "react";
import { api, languageName, type SearchHit } from "./api";
import type { Entry, MapSearchResult, ProjectInfo } from "./types";
import { FolderBrowser, MapNavigationItem } from "./FolderBrowser";

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

export function requestFunctionSearch(query: string, onResults: (hits: SearchHit[]) => void, onError: () => void): () => void {
  let live = true;
  const timer = window.setTimeout(() => {
    api.search(query).then(
      (hits) => live && onResults(hits),
      () => live && onError(),
    );
  }, 150);
  return () => {
    live = false;
    window.clearTimeout(timer);
  };
}

export function requestMapSearch(query: string, onResults: (hits: MapSearchResult[]) => void, onError: () => void): () => void {
  let live = true;
  const timer = window.setTimeout(() => {
    api.mapSearch(query).then(
      (hits) => live && onResults(hits),
      () => live && onError(),
    );
  }, 150);
  return () => {
    live = false;
    window.clearTimeout(timer);
  };
}

interface SearchState {
  query: string;
  status: "pending" | "ready" | "error";
  hits: SearchHit[];
}

interface FileSearchState {
  query: string;
  status: "pending" | "ready" | "error";
  hits: MapSearchResult[];
}

interface Props {
  project: ProjectInfo;
  current: string | null;
  onOpen: (entry: Entry) => void;
  onFunction: (id: string) => void;
  map?: boolean;
  path?: string;
  onMap?: () => void;
  onFile?: (path: string) => void;
  onClose?: () => void;
  modal?: boolean;
}

export function Sidebar({ project, current, onOpen, onFunction, onClose, modal, map = false, path, onMap, onFile }: Props) {
  const [query, setQuery] = useState("");
  const [searchState, setSearchState] = useState<SearchState>({ query: "", status: "ready", hits: [] });
  const [fileSearchState, setFileSearchState] = useState<FileSearchState>({ query: "", status: "ready", hits: [] });
  const [searchAttempt, setSearchAttempt] = useState(0);
  const searchInput = useRef<HTMLInputElement>(null);
  const needle = query.trim().toLowerCase();
  const functionSearch = searchState.query === needle ? searchState : { query: needle, status: "pending", hits: [] };
  const fileSearch = fileSearchState.query === needle ? fileSearchState : { query: needle, status: "pending", hits: [] };
  const search = map ? fileSearch : functionSearch;

  useEffect(() => {
    if (!needle || map) {
      setSearchState({ query: "", status: "ready", hits: [] });
      return;
    }
    setSearchState({ query: needle, status: "pending", hits: [] });
    return requestFunctionSearch(
      needle,
      (results) => setSearchState({ query: needle, status: "ready", hits: results }),
      () => setSearchState({ query: needle, status: "error", hits: [] }),
    );
  }, [needle, searchAttempt, map]);

  useEffect(() => {
    if (!needle || !map) {
      setFileSearchState({ query: "", status: "ready", hits: [] });
      return;
    }
    setFileSearchState({ query: needle, status: "pending", hits: [] });
    return requestMapSearch(
      needle,
      (hits) => setFileSearchState({ query: needle, status: "ready", hits }),
      () => setFileSearchState({ query: needle, status: "error", hits: [] }),
    );
  }, [needle, searchAttempt, map]);

  const matches = (e: Entry) => !needle || e.label.toLowerCase().includes(needle) || e.target.toLowerCase().includes(needle);
  const { stats } = project;
  const knownKinds = new Set(GROUPS.flatMap((group) => group.kinds));
  const allGroups = [...GROUPS, { title: "Other entry points", kinds: project.entries.filter((entry) => !knownKinds.has(entry.kind)).map((entry) => entry.kind) }];
  const groups = allGroups.map((g) => ({ ...g, entries: project.entries.filter((e) => g.kinds.includes(e.kind) && matches(e)) })).filter(
    (g) => g.entries.length > 0,
  );
  const languages = Object.entries(project.inventory?.languages ?? {}).filter(([, count]) => count > 0).sort((a, b) => b[1] - a[1]);

  return (
    <aside className="sidebar" role={modal ? "dialog" : undefined} aria-modal={modal || undefined} aria-label="Project navigation">
      <header className="sidebar-head">
        <div className="brand">
          <svg className="brand-mark" width="30" height="30" viewBox="0 0 30 30" fill="none" aria-hidden="true">
            <rect x="1" y="1" width="28" height="28" rx="8" fill="currentColor" />
            <path d="M10 9v12m0-6h10V9" stroke="var(--surface)" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
            <circle cx="10" cy="9" r="2.5" fill="var(--surface)" />
            <circle cx="10" cy="21" r="2.5" fill="var(--surface)" />
            <circle cx="20" cy="9" r="2.5" fill="var(--surface)" />
          </svg>
          <span className="brand-name">flowmap</span>
          <button className="navigation-close" type="button" aria-label="Close project navigation" onClick={onClose}>
            <span aria-hidden="true">×</span>
          </button>
        </div>
        <div className="project-card">
          <p className="project-eyebrow">Project overview</p>
          <h1 title={project.root}>{project.name}</h1>
          <dl className="project-metrics">
            <div className="project-metric">
              <dt>Files</dt>
              <dd>{number.format(project.inventory?.files ?? stats.files)}</dd>
            </div>
            <div className="project-metric">
              <dt>Functions</dt>
              <dd>{number.format(stats.functions)}</dd>
            </div>
            <div className="project-metric">
              <dt>Entries</dt>
              <dd>{number.format(project.entries.length)}</dd>
            </div>
          </dl>
        </div>
      </header>
      <div className="search-box">
        <svg className="search-icon" width="16" height="16" viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <circle cx="8.5" cy="8.5" r="5.5" stroke="currentColor" strokeWidth="1.5" />
          <path d="m13 13 4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
        <input
          ref={searchInput}
          className="filter"
          type="search"
          placeholder={map ? "Search files and folders…" : "Search entries or functions…"}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label={map ? "Search files and folders" : "Search entry points and functions"}
          autoComplete="off"
          spellCheck={false}
        />
        {query && (
          <button
            className="search-clear"
            aria-label="Clear search"
            title="Clear search"
            onClick={() => {
              setQuery("");
              searchInput.current?.focus();
            }}
          >
            ×
          </button>
        )}
      </div>
      <div className="sidebar-list">
        <ul className="entries">
          <li><button className={`entry project-map-entry${map ? " is-current" : ""}`} aria-current={map ? "page" : undefined} onClick={onMap}>
            <span className="entry-label">Project map</span>
            <span className="entry-meta"><span>Files, folders &amp; dependencies</span><span>{number.format(project.inventory?.files ?? stats.files)} files</span></span>
          </button></li>
        </ul>
        {map && !needle && <FolderBrowser path={path ?? ""} current={path ?? ""} onOpen={(path) => onFile?.(path)} />}
        {map && needle && (
          <section aria-busy={fileSearch.status === "pending"}>
            <h2 className="section-title">Files &amp; folders <span className="count">{fileSearch.status === "pending" ? "…" : fileSearch.hits.length === 50 ? "50+" : fileSearch.hits.length}</span></h2>
            {fileSearch.status === "pending" && <p className="search-state" role="status">Searching files and folders…</p>}
            {fileSearch.status === "error" && <p className="search-state is-error" role="status">Project search is unavailable. <button className="link" onClick={() => setSearchAttempt((attempt) => attempt + 1)}>Retry</button></p>}
            <ul className="entries">
              {fileSearch.hits.map((hit) => <li key={hit.path}>
                <MapNavigationItem item={hit} current={path} showPath onOpen={(path) => {
                  if (hit.category === "folder") setQuery("");
                  onFile?.(path);
                }} />
              </li>)}
            </ul>
            {fileSearch.status === "ready" && fileSearch.hits.length === 50 && <p className="search-state">Showing the first 50 matches. Refine your search to find more.</p>}
            {fileSearch.status === "ready" && fileSearch.hits.length === 0 && <p className="search-state" role="status">No matching files or folders. Try a name or a shorter search.</p>}
          </section>
        )}
        {groups.map((group) => (
          <section key={group.title}>
            <h2 className="section-title">
              {group.title} <span className="count">{group.entries.length}</span>
            </h2>
            <ul className="entries">
              {group.entries.map((entry) => (
                <li key={entry.target}>
                  <button
                    className={entry.target === current ? "entry is-current" : "entry"}
                    aria-current={entry.target === current ? "page" : undefined}
                    onClick={() => onOpen(entry)}
                    title={entry.sources.map((s) => s.text).join("\n")}
                  >
                    <code className="entry-label">{entry.label}</code>
                    <span className="entry-meta">
                      <span className={`kind kind-${entry.kind}`}>{KIND_TEXT[entry.kind] ?? entry.kind}</span>
                      <span>{number.format(entry.reach)} {entry.reach === 1 ? "function" : "functions"}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ))}
        {!map && needle && (
          <section aria-busy={functionSearch.status === "pending"}>
            <h2 className="section-title">
              Functions <span className="count">{functionSearch.status === "pending" ? "…" : functionSearch.hits.length === 30 ? "30+" : functionSearch.hits.length}</span>
            </h2>
            {functionSearch.status === "pending" && <p className="search-state" role="status">Searching functions…</p>}
            {functionSearch.status === "error" && (
              <p className="search-state is-error" role="status">
                Function search is unavailable. <button className="link" onClick={() => setSearchAttempt((attempt) => attempt + 1)}>Retry</button>
              </p>
            )}
            <ul className="entries">
              {functionSearch.hits.map((hit) => (
                <li key={hit.id}>
                  <button
                    className={hit.id === current ? "entry is-current" : "entry"}
                    aria-current={hit.id === current ? "page" : undefined}
                    onClick={() => onFunction(hit.id)}
                    title={hit.doc ?? ""}
                  >
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
            {functionSearch.status === "ready" && functionSearch.hits.length === 0 && groups.length > 0 && <p className="search-state">No matching functions.</p>}
          </section>
        )}
        {!map && needle && search.status === "ready" && groups.length === 0 && functionSearch.hits.length === 0 && (
          <div className="sidebar-empty" role="status">
            <strong>No results for “{query.trim()}”</strong>
            <p>Try a function name, an entry point, or a shorter search.</p>
          </div>
        )}
        {!map && !needle && project.entries.length === 0 && (
          <div className="sidebar-empty">
            <strong>No entry points detected</strong>
            <p>Search for a function to start exploring this project.</p>
            <button className="link" onClick={() => searchInput.current?.focus()}>Find a function →</button>
          </div>
        )}
      </div>
      <footer className="sidebar-footer">
        <span className="project-status"><span className="status-dot" aria-hidden="true" />Local project</span>
        <span className="language-badges">{languages.map(([language, count]) => <span key={language} className="language-badge" title={`${number.format(count)} files`}>{languageName(language)}</span>)}</span>
      </footer>
    </aside>
  );
}
