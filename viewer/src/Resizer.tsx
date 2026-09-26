import { useRef } from "react";

interface Props {
  label: string;
  /** Called with the pointer's horizontal movement since the last call. */
  onDrag: (dx: number) => void;
  onReset: () => void;
}

/** A thin vertical handle between two columns: drag to resize, double-click to reset. */
export function Resizer({ label, onDrag, onReset }: Props) {
  const last = useRef<number | null>(null);
  return (
    <div
      className="resizer"
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      title={`${label} (double-click to reset)`}
      onPointerDown={(event) => {
        last.current = event.clientX;
        event.currentTarget.setPointerCapture(event.pointerId);
        document.body.classList.add("is-resizing");
      }}
      onPointerMove={(event) => {
        if (last.current === null) return;
        onDrag(event.clientX - last.current);
        last.current = event.clientX;
      }}
      onPointerUp={() => {
        last.current = null;
        document.body.classList.remove("is-resizing");
      }}
      onDoubleClick={onReset}
    />
  );
}
