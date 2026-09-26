"""Command line: ``flowmap <folder>``."""

from __future__ import annotations

import argparse
import sys
import threading
import time

from flowmap.entries import Entry
from flowmap.flow import build_flow
from flowmap.mermaid import to_mermaid
from flowmap.project import Project, load_project
from flowmap.server import WEB_DIR, start_viewer


def pick_entry(project: Project, spec: str) -> tuple[str, str | None] | None:
    """(function id, start label) for an entry number, a piece of an entry's
    label or target, or any function id."""
    if spec.isdigit():
        index = int(spec) - 1
        if 0 <= index < len(project.entries):
            entry = project.entries[index]
            return entry.target, entry.label
        return None
    if spec in project.functions:
        return spec, None
    for entry in project.entries:
        if spec in entry.label or spec in entry.target:
            return entry.target, entry.label
    return None


def _entry_line(number: int, entry: Entry) -> str:
    declared = ", ".join(dict.fromkeys(s.kind for s in entry.sources))
    return f"{number:3}  {entry.label:<50}  reach {entry.reach:<4}  {declared}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flowmap", description="Mermaid-style flow diagrams of a code folder.")
    parser.add_argument("path", help="project folder")
    parser.add_argument("--list", action="store_true", help="list the detected entry points")
    parser.add_argument(
        "--mermaid",
        metavar="ENTRY",
        nargs="?",
        const="1",
        help="print a Mermaid flowchart; ENTRY is a number from --list, part of an entry label, or a function id",
    )
    parser.add_argument("--depth", type=int, default=0, help="open steps this many levels deep (with --mermaid)")
    parser.add_argument("--vertical", action="store_true", help="top-down Mermaid instead of left to right")
    parser.add_argument("--port", type=int, default=8765, help="viewer port (a free one is used if taken)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    print(f"flowmap: reading {args.path} …", file=sys.stderr)
    project = load_project(args.path)
    print(
        f"flowmap: {len(project.modules)} files, {len(project.functions)} functions, "
        f"{len(project.entries)} entry points ({time.perf_counter() - started:.1f}s)",
        file=sys.stderr,
    )

    if args.mermaid:
        picked = pick_entry(project, args.mermaid)
        if picked is None:
            print(f"flowmap: no entry matches {args.mermaid!r} (see --list)", file=sys.stderr)
            return 2
        target, label = picked
        graph = build_flow(project, target, depth=args.depth, start_label=label)
        sys.stdout.write(to_mermaid(graph, direction="TD" if args.vertical else "LR"))
        return 0

    if args.list:
        for number, entry in enumerate(project.entries, 1):
            print(_entry_line(number, entry))
        return 0

    if not (WEB_DIR / "index.html").is_file():
        print("flowmap: the viewer is not built yet (run `npm install && npm run build` in viewer/)", file=sys.stderr)
    server, url = start_viewer(project, port=args.port, open_browser=not args.no_open)
    print(f"flowmap: {project.name}: {len(project.entries)} entry points at {url}  (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
