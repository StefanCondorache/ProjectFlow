import type { CSSProperties } from "react";

const paths = {
  panels: "M9 3v18M3 3h18v18H3zM5.5 7h1M5.5 11h1",
  back: "M19 12H5m7-7-7 7 7 7",
  code: "m8 8-4 4 4 4m8-8 4 4-4 4m-3-11-2 14",
  layout: "M4 5h5v5H4zM15 14h5v5h-5zM6.5 10v6.5H15M13 4h7M17 1l3 3-3 3",
  expand: "M4 9V4h5m6 0h5v5M4 15v5h5m6 0h5v-5",
  collapse: "M9 4v5H4m16 0h-5V4M4 15h5v5m6 0v-5h5",
  play: "m8 5 11 7-11 7z",
  stop: "M6 6h12v12H6z",
  help: "M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2-3 4m.1 3h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0",
  flow: "M4 4h6v6H4zM14 14h6v6h-6zM7 10v7h7M14 5h6m-3-3 3 3-3 3",
  close: "m6 6 12 12M6 18 18 6",
  check: "m5 12 4 4L19 6",
} as const;

export function Icon({ name, size = 16, style }: { name: keyof typeof paths; size?: number; style?: CSSProperties }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={style}>
      <path d={paths[name]} />
    </svg>
  );
}
