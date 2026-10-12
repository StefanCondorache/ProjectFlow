import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement, type ComponentProps } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { api, readHash, writeHash, type ViewState } from "./api";
import { Sidebar } from "./Sidebar";
import * as Browser from "./FolderBrowser";
import type { MapChildrenPage, ProjectInfo } from "./types";

beforeEach(() => {
  const location = { hash: "" };
  vi.stubGlobal("location", location);
  vi.stubGlobal("history", {
    pushState: (_state: unknown, _title: string, url: string) => { location.hash = url; },
    replaceState: (_state: unknown, _title: string, url: string) => { location.hash = url; },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const page: MapChildrenPage = {
  path: "lib,common", offset: 40, limit: 40, total: 81,
  items: [
    { path: "lib,common/订单", label: "订单", category: "folder", language: "" },
    { path: "lib,common/订单.ts", label: "订单.ts", category: "module", language: "typescript" },
  ],
};

describe("folder inventory requests", () => {
  it("requests encoded file paths with a bounded default page", async () => {
    const requests: string[] = [];
    vi.stubGlobal("fetch", async (url: string) => { requests.push(url); return new Response(JSON.stringify(page)); });
    expect(await api.mapChildren("lib,common/订单.ts", 40)).toEqual(page);
    expect(requests).toEqual(["api/map-children?path=lib%2Ccommon%2F%E8%AE%A2%E5%8D%95.ts&offset=40&limit=40"]);
  });

  it("cannot publish a canceled path or page over the current page", async () => {
    let finishOld!: (response: Response) => void;
    let finishNew!: (response: Response) => void;
    vi.stubGlobal("fetch", (url: string) => new Promise<Response>((resolve) => {
      if (url.includes("offset=0")) finishOld = resolve;
      else finishNew = resolve;
    }));
    const displayed: MapChildrenPage[] = [];
    const cancelOld = Browser.requestMapChildren("lib,common", 0, (result) => displayed.push(result), () => undefined);
    cancelOld();
    const cancelNew = Browser.requestMapChildren("lib,common", 40, (result) => displayed.push(result), () => undefined);
    finishNew(new Response(JSON.stringify(page)));
    await vi.waitFor(() => expect(displayed).toEqual([page]));
    finishOld(new Response(JSON.stringify({ ...page, offset: 0 })));
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(displayed).toEqual([page]);
    cancelNew();
  });

  it("reports active errors while discarding a canceled error", async () => {
    let failOld!: (error: Error) => void;
    vi.stubGlobal("fetch", (url: string) => url.includes("offset=0")
      ? new Promise<Response>((_resolve, reject) => { failOld = reject; })
      : Promise.resolve(new Response("unavailable", { status: 503 })));
    const failures: string[] = [];
    const cancelOld = Browser.requestMapChildren("old", 0, () => undefined, () => failures.push("old"));
    cancelOld();
    Browser.requestMapChildren("new", 40, () => undefined, () => failures.push("new"));
    await vi.waitFor(() => expect(failures).toEqual(["new"]));
    failOld(new Error("unavailable"));
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(failures).toEqual(["new"]);
  });
});

describe("source line bookmarks", () => {
  it("round trips a cited file and individually encoded open folders", () => {
    const view: ViewState = { mode: "map", root: "@project", path: "lib,common/订单.ts", line: 150, expanded: ["dir:lib,common"] };
    writeHash(view, "push");
    expect(readHash()).toEqual(view);
    location.hash = "#mode=map&path=src%2Fa.ts&line=150";
    expect(readHash()?.line).toBe(150);
  });

  it.each(["0", "-1", "1.5", "NaN", "Infinity", "9007199254740992", "", "1e2", "0x10"])("discards an invalid map line %s", (line) => {
    location.hash = `#mode=map&path=src%2Fa.ts&line=${line}`;
    expect(readHash()).toEqual({ mode: "map", root: "@project", path: "src/a.ts", expanded: [] });
  });

  it.each([0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1])("does not write an invalid map line %s", (line) => {
    writeHash({ mode: "map", root: "@project", path: "src/a.ts", expanded: [], line }, "replace");
    expect(new URLSearchParams(location.hash.slice(1)).has("line")).toBe(false);
  });

  it("keeps lines out of flow bookmarks and graph requests", async () => {
    location.hash = "#root=app.py%3A%3Amain&expand=s%2F1&line=150";
    expect(readHash()).toEqual({ root: "app.py::main", expanded: ["s/1"], start: undefined, at: undefined, var: undefined, trailView: undefined });
    writeHash({ root: "app.py::main", expanded: ["s/1"], line: 150 }, "replace");
    expect(new URLSearchParams(location.hash.slice(1)).has("line")).toBe(false);
    const requests: string[] = [];
    vi.stubGlobal("fetch", async (url: string) => { requests.push(url); return new Response(JSON.stringify({ root: "@project", nodes: [], edges: [] })); });
    await api.flow({ mode: "map", root: "@project", path: "src/a.ts", expanded: [], line: 150 });
    await api.flow({ root: "app.py::main", expanded: [], line: 150 });
    expect(requests).toEqual(["api/overview?path=src%2Fa.ts", "api/flow?root=app.py%3A%3Amain&expand="]);
  });
});

describe("folder browser presentation", () => {
  it("preserves a page when returning from a file to its canonical folder", () => {
    expect(Browser.folderPageOffset(page, "lib,common")).toBe(40);
  });

  it("preserves the page for a listed file while new folders start at page one", () => {
    expect(Browser.folderPageOffset(page, "lib,common/订单.ts")).toBe(40);
    expect(Browser.folderPageOffset(page, "lib,common/订单")).toBe(0);
    expect(Browser.folderPageOffset(page, "other")).toBe(0);
    expect(Browser.folderPageOffset(undefined, "lib,common")).toBe(0);
  });

  it("shows the canonical parent folder, truthful range and focused file marker", () => {
    const html = renderToStaticMarkup(createElement(Browser.FolderBrowserView, {
      state: { status: "ready", page }, current: "lib,common/订单.ts", onOpen: () => undefined, onPage: () => undefined, onRetry: () => undefined,
    }));
    expect(html).toContain("Browse folder");
    expect(html).toContain("lib,common");
    expect(html).toContain("41–42 of 81");
    expect(html).toContain("Previous");
    expect(html).toContain("Next");
    expect(html).toContain("Up");
    expect(html).toContain("Folder");
    expect(html).toContain("TypeScript");
    expect(html).toContain("Current");
    expect(html).toContain('aria-current="page"');
  });

  it("disables unavailable pages and has no Up control at project root", () => {
    const html = renderToStaticMarkup(createElement(Browser.FolderBrowserView, {
      state: { status: "ready", page: { path: "", offset: 0, limit: 40, total: 1, items: [page.items[1]] } }, current: "", onOpen: () => undefined, onPage: () => undefined, onRetry: () => undefined,
    }));
    expect(html).toContain("Project root");
    expect(html).toContain("1–1 of 1");
    expect(html).not.toContain('aria-label="Up to parent folder"');
    expect(html.match(/disabled=""/g)).toHaveLength(2);
  });

  it.each([
    [{ status: "pending", path: "empty" }, "Loading folder…"],
    [{ status: "error", path: "empty" }, "Folder browsing is unavailable."],
    [{ status: "ready", page: { path: "empty", offset: 0, limit: 40, total: 0, items: [] } }, "This folder is empty."],
  ] satisfies [ComponentProps<typeof Browser.FolderBrowserView>["state"], string][])("presents a distinct state %j", (state, message) => {
    const html = renderToStaticMarkup(createElement(Browser.FolderBrowserView, {
      state, current: "empty", onOpen: () => undefined, onPage: () => undefined, onRetry: () => undefined,
    }));
    expect(html).toContain(message);
    expect(html).toContain('role="status"');
    if (state.status === "error") expect(html).toContain("Retry");
    if (state.status === "pending") expect(html).toContain('aria-busy="true"');
  });

  it("adds browsing only to map navigation and makes folder search discoverable", () => {
    const project: ProjectInfo = { name: "Example", root: "/example", files: [], stats: { files: 0, functions: 0, classes: 0, calls: 0, project_calls: 0 }, entries: [] };
    const props = { project, current: null, onOpen: () => undefined, onFunction: () => undefined };
    const mapHtml = renderToStaticMarkup(createElement(Sidebar, { ...props, map: true, path: "empty", onFile: () => undefined }));
    expect(mapHtml).toContain("Browse folder");
    expect(mapHtml).toContain("Search files and folders");
    expect(renderToStaticMarkup(createElement(Sidebar, props))).not.toContain("Browse folder");
  });
});
