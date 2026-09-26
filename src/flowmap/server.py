"""Local web server for the viewer: the built viewer files plus a small JSON API.

It binds to 127.0.0.1 and only ever reads the analysed project.

    GET /api/project                         entries, files, stats
    GET /api/flow?root=&expand=&depth=&start=  diagram of a function
    GET /api/trail?root=&at=&var=&view=      one piece of data: its journey (functions
                                             by file) or, with view=steps, every step
    GET /api/mermaid?(as flow or trail)      the same diagram as Mermaid text
    GET /api/source?file=&start=&end=        lines of an analysed source file
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
from flowmap.flow import Graph, build_flow
from flowmap.journey import build_journey
from flowmap.ir import Call, walk
from flowmap.mermaid import to_mermaid
from flowmap.project import Project

WEB_DIR = Path(__file__).parent / "web"
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
        }
        try:
            route = routes.get(url.path)
            if route is not None:
                route(query)
            else:
                self._static(unquote(url.path))
        except NotFound as err:
            self._send(404, "application/json", json.dumps({"error": str(err)}).encode())
        except (ValueError, KeyError) as err:
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
        self._send(200, "text/plain; charset=utf-8", to_mermaid(self._graph(query)[0]).encode())

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
