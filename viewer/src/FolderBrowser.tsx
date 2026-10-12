import { useEffect, useState } from "react";
import { api, languageName } from "./api";
import { Icon } from "./Icon";
import type { MapChildrenPage, MapSearchResult } from "./types";

type BrowserState =
  | { status: "pending" | "error"; path: string }
  | { status: "ready"; page: MapChildrenPage };

function isFolderPageFocus(page: MapChildrenPage | undefined, path: string): boolean {
  return Boolean(page && (page.path === path || page.items.some((item) => item.path === path && item.category !== "folder")));
}

export function folderPageOffset(page: MapChildrenPage | undefined, path: string): number {
  return page && isFolderPageFocus(page, path) ? page.offset : 0;
}

export function requestMapChildren(path: string, offset: number, onResult: (page: MapChildrenPage) => void, onError: () => void): () => void {
  let live = true;
  api.mapChildren(path, offset).then(
    (page) => live && onResult(page),
    () => live && onError(),
  );
  return () => { live = false; };
}

export function MapNavigationItem({ item, current, onOpen, showPath }: { item: MapSearchResult; current?: string; onOpen: (path: string) => void; showPath?: boolean }) {
  const folder = item.category === "folder";
  const selected = item.path === current;
  return (
    <button type="button" className={`entry map-navigation-item${selected ? " is-current" : ""}`} aria-label={`Open ${folder ? "folder" : "file"} ${item.path}`} aria-current={selected ? "page" : undefined} onClick={() => onOpen(item.path)} title={item.path}>
      <span className="map-item-head"><Icon name={folder ? "folder" : "file"} /><code className="entry-label">{item.label}</code>{selected && <span className="folder-current">Current</span>}</span>
      <span className="entry-meta"><span>{folder ? "Folder" : languageName(item.language)}</span>{!folder && <span>{item.category}</span>}</span>
      {showPath && <code className="map-search-path">{item.path}</code>}
    </button>
  );
}

export function FolderBrowserView({ state, current, onOpen, onPage, onRetry }: {
  state: BrowserState;
  current: string;
  onOpen: (path: string) => void;
  onPage: (offset: number) => void;
  onRetry: () => void;
}) {
  const page = state.status === "ready" ? state.page : undefined;
  const path = state.status === "ready" ? state.page.path : state.path;
  const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
  const range = page ? (page.items.length ? `${page.offset + 1}–${page.offset + page.items.length} of ${page.total}` : `0 of ${page.total}`) : "";
  return (
    <section className="folder-browser" aria-label="Browse folder" aria-busy={state.status === "pending"}>
      <h2 className="section-title">Browse folder</h2>
      <div className="folder-location"><code title={path || "Project root"}>{path || "Project root"}</code>{page && path && <button type="button" aria-label="Up to parent folder" onClick={() => onOpen(parent)}><Icon name="up" size={13} />Up</button>}</div>
      {state.status === "pending" && <p className="search-state" role="status">Loading folder…</p>}
      {state.status === "error" && <p className="search-state is-error" role="status">Folder browsing is unavailable. <button type="button" className="link" onClick={onRetry}>Retry</button></p>}
      {page && <>
        <ul className="entries">{page.items.map((item) => <li key={item.path}><MapNavigationItem item={item} current={current} onOpen={onOpen} /></li>)}</ul>
        {page.items.length === 0 && <p className="search-state" role="status">{page.total === 0 ? "This folder is empty." : "No items on this page. Use Previous to return."}</p>}
      </>}
      <div className="folder-pages">
        <p role="status">{page ? `${range} items` : ""}</p>
        <div>
          <button type="button" disabled={!page || page.offset === 0} onClick={() => page && onPage(Math.max(0, page.offset - page.limit))}>Previous</button>
          <button type="button" disabled={!page || page.offset + page.limit >= page.total} onClick={() => page && onPage(page.offset + page.limit)}>Next</button>
        </div>
      </div>
    </section>
  );
}

export function FolderBrowser({ path, current, onOpen }: { path: string; current: string; onOpen: (path: string) => void }) {
  const [paging, setPaging] = useState({ focus: path, path, offset: 0 });
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{ key: string; state: BrowserState }>({ key: "", state: { status: "pending", path } });
  if (paging.focus !== path) {
    // Keep the listed page within its folder; a new folder starts at page one.
    const page = result.key === JSON.stringify([paging.path, paging.offset, attempt]) && result.state.status === "ready" ? result.state.page : undefined;
    setPaging({ focus: path, path: isFolderPageFocus(page, path) ? paging.path : path, offset: folderPageOffset(page, path) });
  }
  const browsePath = paging.focus === path ? paging.path : path;
  const offset = paging.focus === path ? paging.offset : 0;
  const key = JSON.stringify([browsePath, offset, attempt]);
  const state: BrowserState = result.key === key ? result.state : { status: "pending", path: browsePath };
  useEffect(() => {
    setResult({ key, state: { status: "pending", path: browsePath } });
    return requestMapChildren(browsePath, offset,
      (page) => setResult({ key, state: { status: "ready", page } }),
      () => setResult({ key, state: { status: "error", path: browsePath } }),
    );
  }, [browsePath, offset, key]);
  return <FolderBrowserView state={state} current={current} onOpen={onOpen} onPage={(offset) => setPaging({ ...paging, offset })} onRetry={() => setAttempt((value) => value + 1)} />;
}
