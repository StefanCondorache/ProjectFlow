// Node sizes for the layout, measured with the same fonts the CSS uses.
import type { Size, Sizing } from "./layout";
import type { FlowNode } from "./types";

const FAMILY = 'system-ui, -apple-system, "Segoe UI", sans-serif';
const MONO = 'ui-monospace, "SF Mono", Menlo, Consolas, monospace';
const FONTS = {
  title: `600 13px ${FAMILY}`,
  body: `12px ${FAMILY}`,
  bodyBold: `600 12px ${FAMILY}`,
  small: `11px ${FAMILY}`,
  code: `12px ${MONO}`,
};

let context: CanvasRenderingContext2D | null = null;

export function textWidth(text: string, font: keyof typeof FONTS): number {
  context ??= document.createElement("canvas").getContext("2d");
  if (!context) return text.length * 7;
  context.font = FONTS[font];
  return context.measureText(text).width;
}

const clamp = (value: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, value));

export function headerText(node: FlowNode): string {
  switch (node.kind) {
    case "group":
      return `${node.label}  ${node.detail.file ?? ""}`;
    case "file":
      return `▣ ${node.label}`;
    case "loop":
      return `↻ ${node.label}`;
    case "handler":
      return `⚠ ${node.label}`;
    default:
      return node.label;
  }
}

export function nodeSize(node: FlowNode): Size {
  const d = node.detail;
  switch (node.kind) {
    case "start":
    case "end":
      return d.inner
        ? { width: clamp(textWidth(node.label, "small") + 28, 64, 260), height: 24 }
        : { width: clamp(textWidth(node.label, "title") + 44, 96, 380), height: 38 };
    case "step": {
      const second = d.receives ? `receives ${d.receives.join(", ")}` : (d.file ?? "");
      const widest = Math.max(
        textWidth(node.label, "title") + 30 + (d.returns ? 16 : 0),
        textWidth(second, "small"),
        d.doc ? Math.min(textWidth(d.doc, "body"), 250) : 0,
      );
      return { width: clamp(widest + 30, 150, 310), height: d.doc ? 70 : 52 };
    }
    case "io":
      return { width: clamp(textWidth(node.label, "code") + 60, 170, 340), height: 46 };
    case "use":
      return { width: clamp(textWidth(node.label, "code") + 32, 120, 340), height: 32 };
    case "decision":
      return { width: clamp(textWidth(node.label, "code") * 1.35 + 56, 120, 320), height: 64 };
    case "raise":
      return { width: clamp(textWidth(node.label, "bodyBold") + 56, 110, 320), height: 34 };
    default:
      return { width: 0, height: 0 };
  }
}

export const sizing: Sizing = {
  node: nodeSize,
  label: (text) => ({ width: Math.min(textWidth(text, "small"), 180) + 14, height: 18 }),
  header: (node) => textWidth(headerText(node), "small") + 64,
};
