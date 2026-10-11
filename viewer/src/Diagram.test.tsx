import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { Diagram } from "./Diagram";
import type { FlowNode } from "./types";

function cardTitles(nodes: FlowNode[]): string[] {
  const markup = renderToStaticMarkup(
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
