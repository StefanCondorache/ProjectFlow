import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type SearchHit } from "./api";
import { requestFunctionSearch } from "./Sidebar";
import { requestNodeSource } from "./Details";
import type { SourceLines } from "./types";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("function search requests", () => {
  it("keeps a late response for the previous query out of the current results", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("window", globalThis);
    const oldRequest = deferred<SearchHit[]>();
    const newRequest = deferred<SearchHit[]>();
    vi.spyOn(api, "search").mockImplementation((query) => (query === "old" ? oldRequest.promise : newRequest.promise));
    const displayed: SearchHit[][] = [];
    const oldHit: SearchHit = { id: "old", label: "old", file: "old.py", line: 1, doc: null };
    const newHit: SearchHit = { id: "new", label: "new", file: "new.py", line: 2, doc: null };

    const cancelOld = requestFunctionSearch("old", (hits) => displayed.push(hits), () => undefined);
    await vi.advanceTimersByTimeAsync(150);
    cancelOld();
    const cancelNew = requestFunctionSearch("new", (hits) => displayed.push(hits), () => undefined);
    await vi.advanceTimersByTimeAsync(150);
    newRequest.resolve([newHit]);
    await Promise.resolve();
    oldRequest.resolve([oldHit]);
    await Promise.resolve();

    expect(displayed).toEqual([[newHit]]);
    cancelNew();
  });

  it("reports a search failure only while that query is active", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("window", globalThis);
    const active = deferred<SearchHit[]>();
    const canceled = deferred<SearchHit[]>();
    vi.spyOn(api, "search").mockImplementation((query) => (query === "active" ? active.promise : canceled.promise));
    const failures: string[] = [];
    const cancelActive = requestFunctionSearch("active", () => undefined, () => failures.push("active"));
    await vi.advanceTimersByTimeAsync(150);
    active.reject(new Error("unavailable"));
    await Promise.resolve();
    await Promise.resolve();
    const cancelCanceled = requestFunctionSearch("canceled", () => undefined, () => failures.push("canceled"));
    await vi.advanceTimersByTimeAsync(150);
    cancelCanceled();
    canceled.reject(new Error("unavailable"));
    await Promise.resolve();
    await Promise.resolve();

    expect(failures).toEqual(["active"]);
    cancelActive();
  });
});

describe("inspector source requests", () => {
  it("keeps source from a previously selected node out of the inspector", async () => {
    const oldRequest = deferred<SourceLines>();
    const newRequest = deferred<SourceLines>();
    vi.spyOn(api, "source").mockImplementation((file) => (file === "old.py" ? oldRequest.promise : newRequest.promise));
    const displayed: SourceLines[] = [];
    const newest: SourceLines = { file: "new.py", start: 12, lines: ["def new():"] };

    const cancelOld = requestNodeSource({ file: "old.py", from: 1 }, (source) => displayed.push(source), () => undefined);
    cancelOld();
    const cancelNew = requestNodeSource({ file: "new.py", from: 12 }, (source) => displayed.push(source), () => undefined);
    newRequest.resolve(newest);
    await Promise.resolve();
    oldRequest.resolve({ file: "old.py", start: 1, lines: ["def old():"] });
    await Promise.resolve();

    expect(displayed).toEqual([newest]);
    cancelNew();
  });

  it("reports a failed source request without updating an inspector that has closed", async () => {
    const active = deferred<SourceLines>();
    const canceled = deferred<SourceLines>();
    vi.spyOn(api, "source").mockImplementation((file) => (file === "active.py" ? active.promise : canceled.promise));
    const failures: string[] = [];
    const cancelActive = requestNodeSource({ file: "active.py", from: 1 }, () => undefined, () => failures.push("active"));
    active.reject(new Error("unavailable"));
    await Promise.resolve();
    await Promise.resolve();
    const cancelCanceled = requestNodeSource({ file: "canceled.py", from: 1 }, () => undefined, () => failures.push("canceled"));
    cancelCanceled();
    canceled.reject(new Error("unavailable"));
    await Promise.resolve();
    await Promise.resolve();

    expect(failures).toEqual(["active"]);
    cancelActive();
  });
});
