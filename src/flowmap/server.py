"""Local web server for the viewer: the built viewer files plus a small JSON API.

It binds to 127.0.0.1 and only ever reads the analysed project.

    GET /api/project                         entries, files, stats
    GET /api/flow?root=&expand=&depth=&start=  diagram of a function
    GET /api/trail?root=&at=&var=&view=      one piece of data: its journey (functions
                                             by file) or, with view=steps, every step
    GET /api/mermaid?(as flow or trail)&dir=  the same diagram as Mermaid text (dir: LR or TD)
    GET /api/source?file=&start=&end=        lines of an analysed source file
    GET /api/search?q=                       functions whose name matches, best first
    GET /api/simulate?root=&expand=&start=&choices=node=yes,...
                                             walk the diagram with its data, frame by frame
    POST /api/simulate                       the same, with real data: a JSON object with
                                             root, expand, start, choices, inputs, env, argv,
                                             provided, start_at, auto_open
    GET /api/expects?root=&expand=&at=       the variables at a node (for a walk started
                                             there), plus the env vars and command line read
"""

from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from flowmap.dataflow import trace
from flowmap.flow import Graph, build_flow, display_name
from flowmap.journey import build_journey
from flowmap.ir import Call, walk
from flowmap.mermaid import to_mermaid
from flowmap.project import Project
from flowmap.simulate import expects, simulate

WEB_DIR = Path(__file__).parent / "web"
MAX_BODY = 5_000_000
_TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}


class NotFound(Exception):
    pass


def make_server(project: Project, web_dir: Path = WEB_DIR, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"project": project, "web_dir": Path(web_dir).resolve()})
    return ThreadingHTTPServer((host, port), handler)


def start_viewer(
    project: Project, port: int = 8765, open_browser: bool = True, web_dir: Path = WEB_DIR
) -> tuple[ThreadingHTTPServer, str]:
    """Serve the viewer in a background thread; returns the server and its URL.
    Falls back to any free port when ``port`` is taken."""
    try:
        server = make_server(project, web_dir, port=port)
    except OSError:
        server = make_server(project, web_dir, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    if open_browser:
        webbrowser.open(url)
    return server, url


def project_summary(project: Project) -> dict:
    calls = [c for fn in project.functions.values() for c in walk(fn.body) if isinstance(c, Call)]
    to_project = sum(c.kind in ("project", "class") for c in calls)
    return {
        "name": project.name,
        "root": str(project.root),
        "files": sorted(project.modules),
        "stats": {
            "files": len(project.modules),
            "functions": len(project.functions),
            "classes": len(project.classes),
            "calls": len(calls),
            "project_calls": to_project,
        },
        "entries": [asdict(e) for e in project.entries],
    }


def _typed(body: dict, key: str, kind: type, default):
    value = body.get(key)
    if value is None:
        return default
    if not isinstance(value, kind):
        raise ValueError(f"{key}: expected {kind.__name__}")
    return value


class _Handler(BaseHTTPRequestHandler):
    project: Project
    web_dir: Path
    server_version = "flowmap"

    def log_message(self, format, *args):  # keep the terminal quiet
        pass

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        routes = {
            "/api/project": self._project,
            "/api/flow": self._flow,
            "/api/trail": self._flow,
            "/api/mermaid": self._mermaid,
            "/api/source": self._source,
            "/api/simulate": self._simulate,
            "/api/expects": self._expects,
            "/api/search": self._search,
        }
        route = routes.get(url.path)
        self._answer(lambda: route(query) if route is not None else self._static(unquote(url.path)))

    def do_POST(self) -> None:
        url = urlsplit(self.path)

        def answer() -> None:
            if url.path != "/api/simulate":
                raise NotFound(url.path)
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                raise ValueError("expected a JSON body")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            self._run_simulation(body)

        self._answer(answer)

    def _answer(self, respond) -> None:
        try:
            respond()
        except NotFound as err:
            self._send(404, "application/json", json.dumps({"error": str(err)}).encode())
        except (ValueError, KeyError, TypeError) as err:  # json errors are ValueErrors
            self._send(400, "application/json", json.dumps({"error": str(err)}).encode())

    # responses

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data) -> None:
        self._send(200, "application/json", json.dumps(data).encode())

    # routes

    def _project(self, query: dict) -> None:
        self._json(project_summary(self.project))

    def _graph(self, query: dict) -> tuple[Graph, dict | None]:
        root = query.get("root", "")
        if root not in self.project.functions:
            raise NotFound(f"unknown function {root!r}")
        start = query.get("start") or None
        if query.get("at") and query.get("var"):
            trail = trace(self.project, root, query["at"], query["var"])
            if not trail.found:
                raise NotFound(f"no {query['var']!r} at {query['at']!r}")
            view = "steps" if query.get("view") == "steps" else "journey"
            info = {"origin": trail.origin, "var": trail.var, "truncated": trail.truncated, "view": view}
            if view == "journey":
                return build_journey(self.project, trail), info
            return build_flow(self.project, root, start_label=start, trail=trail), info
        expanded = {part for part in query.get("expand", "").split(",") if part}
        return build_flow(self.project, root, expanded=expanded, depth=int(query.get("depth", 0)), start_label=start), None

    def _flow(self, query: dict) -> None:
        graph, trail = self._graph(query)
        body = {"root": graph.root, "nodes": [asdict(n) for n in graph.nodes], "edges": [asdict(e) for e in graph.edges]}
        if trail is not None:
            body["trail"] = trail
        self._json(body)

    def _mermaid(self, query: dict) -> None:
        text = to_mermaid(self._graph(query)[0], direction=query.get("dir", "LR"))
        self._send(200, "text/plain; charset=utf-8", text.encode())

    def _search(self, query: dict) -> None:
        needle = query.get("q", "").strip().lower()
        if not needle:
            self._json([])
            return
        ranked = []
        for fn in self.project.functions.values():
            if fn.name == "<main>":
                continue
            name, qualname = fn.name.lower(), fn.qualname.lower()
            if name == needle:
                rank = 0
            elif name.startswith(needle):
                rank = 1
            elif needle in qualname:
                rank = 2
            else:
                continue
            ranked.append((rank, len(fn.qualname), fn.file, fn.qualname, fn))
        ranked.sort(key=lambda r: r[:4])
        hits = [
            {"id": fn.id, "label": display_name(self.project, fn), "file": fn.file, "line": fn.line, "doc": fn.doc}
            for *_, fn in ranked[:30]
        ]
        self._json(hits)

    def _simulate(self, query: dict) -> None:
        self._run_simulation(
            {
                "root": query.get("root", ""),
                "expand": [part for part in query.get("expand", "").split(",") if part],
                "start": query.get("start") or None,
                "choices": dict(part.rsplit("=", 1) for part in query.get("choices", "").split(",") if "=" in part),
            }
        )

    def _run_simulation(self, body: dict) -> None:
        root = body.get("root")
        if root not in self.project.functions:
            raise NotFound(f"unknown function {root!r}")
        argv = _typed(body, "argv", list, None)
        sim = simulate(
            self.project,
            root,
            expanded=set(_typed(body, "expand", list, [])),
            start_label=_typed(body, "start", str, None),
            choices={str(k): str(v) for k, v in _typed(body, "choices", dict, {}).items()},
            inputs=_typed(body, "inputs", dict, {}),
            env=_typed(body, "env", dict, None),
            argv=[str(a) for a in argv] if argv is not None else None,
            provided=_typed(body, "provided", dict, {}),
            start_at=_typed(body, "start_at", str, None),
            auto_open=bool(_typed(body, "auto_open", bool, False)),
        )
        self._json(
            {
                "status": sim.status,
                "choice": sim.choice,
                "error": sim.error,
                "expanded": sim.expanded,
                "frames": [asdict(f) for f in sim.frames],
            }
        )

    def _expects(self, query: dict) -> None:
        root = query.get("root", "")
        if root not in self.project.functions:
            raise NotFound(f"unknown function {root!r}")
        expanded = {part for part in query.get("expand", "").split(",") if part}
        self._json(expects(self.project, root, expanded, query.get("at") or "s"))

    def _source(self, query: dict) -> None:
        file = query.get("file", "")
        if file not in self.project.modules:
            raise NotFound(f"not an analysed file: {file!r}")
        lines = (self.project.root / file).read_text(errors="replace").splitlines()
        start = max(1, int(query.get("start", 1)))
        end = min(len(lines), int(query.get("end", len(lines))))
        self._json({"file": file, "start": start, "lines": lines[start - 1 : end]})

    def _static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        target = (self.web_dir / rel).resolve()
        if not target.is_relative_to(self.web_dir) or not target.is_file():
            raise NotFound(path)
        content_type = _TYPES.get(target.suffix) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, content_type, target.read_bytes())
