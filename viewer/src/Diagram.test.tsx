import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { Diagram } from "./Diagram";
import type { FlowNode } from "./types";

function cardMarkup(nodes: FlowNode[]): string {
  return renderToStaticMarkup(
    <Diagram
      placed={{ width: 520, height: 130, nodes: nodes.map((node, i) => ({ node, x: i * 260, y: 0, width: 240, height: 90, depth: 0 })), edges: [] }}
      rootKey="@project"
      anchor={null}
      fitSignal={0}
      selected={null}
      onSelect={() => undefined}
      onToggle={() => undefined}
      direction="RIGHT"
      following={false}
      map
    />,
  );
}

function cardTitles(nodes: FlowNode[]): string[] {
  const markup = cardMarkup(nodes);
  return [...markup.matchAll(/<span class="map-card-title">([^<]*)<\/span>/g)].map((match) => match[1]);
}

it("preserves external package scopes so packages with the same basename remain distinct", () => {
  expect(cardTitles([
    { id: "external:@org-a/core", kind: "external", label: "@org-a/core", parent: null, detail: {} },
    { id: "external:@org-b/core", kind: "external", label: "@org-b/core", parent: null, detail: {} },
  ])).toEqual(["@org-a/core", "@org-b/core"]);
});

it("keeps project file cards readable using their basename", () => {
  expect(cardTitles([
    { id: "file:src/app.ts", kind: "module", label: "app.ts", parent: null, detail: { path: "src/app.ts" } },
  ])).toEqual(["app.ts"]);
});

it("names the language and labels partially analyzed source cards honestly", () => {
  const markup = cardMarkup([
    { id: "file:partial.ts", kind: "module", label: "partial.ts", parent: null,
      detail: { path: "partial.ts", language: "typescript", analysis: "partial", functions: [{ id: "partial.ts::route@1:7", label: "route", line: 1 }] } },
  ]);
  expect(markup).toContain('<span class="map-card-type">TypeScript</span>');
  expect(markup).toContain("<span>Partial analysis</span>");
  expect(markup).not.toContain("<span>Structure</span>");
});
