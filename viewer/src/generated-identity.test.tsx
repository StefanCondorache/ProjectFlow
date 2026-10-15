import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { Diagram } from "./Diagram";
import type { FlowEdge } from "./types";

function chips(generated?: string[] | null): string {
  const edge: FlowEdge & { generated_data?: string[] | null } = {
    source: "s", target: "e", label: "$input, $1, result", kind: "flow",
    data: ["$input", "$1", "$2"], generated_data: generated,
  };
  return renderToStaticMarkup(<Diagram
    placed={{ width: 520, height: 130, nodes: [], edges: [{
      id: "edge", edge, points: [{ x: 0, y: 0 }, { x: 300, y: 0 }],
      label: { text: edge.label, x: 100, y: 0, width: 200, height: 24 },
    }] }}
    rootKey="app.js::run" anchor={null} fitSignal={0} selected={null}
    onSelect={() => undefined} onToggle={() => undefined} onFollow={() => undefined}
    direction="RIGHT" following={false}
  />);
}

it("renders real dollar names and only identifies explicit generated edge data as results", () => {
  const markup = chips(["$2"]);
  expect(markup).toContain('title="Follow $input through the code">$input</button>');
  expect(markup).toContain('title="Follow $1 through the code">$1</button>');
  expect(markup).toContain('title="Follow this result through the code">result</button>');
});

it("treats an explicit empty generated list as all user data", () => {
  expect(chips([])).toContain('title="Follow $2 through the code">$2</button>');
});

it("keeps legacy result rendering when identity metadata is absent", () => {
  expect(chips()).not.toContain('title="Follow $1 through the code"');
  expect(chips(null)).toContain('title="Follow this result through the code">result</button>');
});

it("uses origin identity for the Following banner while retaining legacy payload support", async () => {
  const { followingLabel } = await import('./data-labels');
  expect(followingLabel('$1', false)).toBe('$1');
  expect(followingLabel('$value', false)).toBe('$value');
  expect(followingLabel('$2', true)).toBe("a call's result");
  expect(followingLabel('$2')).toBe("a call's result");
  expect(followingLabel('value')).toBe('value');
});

it("keeps a non-Python dollar name visible while its trail metadata is loading", async () => {
  const { followingLabel } = await import('./data-labels');
  expect(followingLabel('$input', undefined, 'javascript')).toBe('$input');
  expect(followingLabel('$input', null, 'typescript')).toBe('$input');
  expect(followingLabel('$2', undefined, 'python')).toBe("a call's result");
  expect(followingLabel('$2', true, 'javascript')).toBe("a call's result");
});
