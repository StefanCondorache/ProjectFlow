import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

import pytest

from flowmap.project import load_project
from flowmap.server import make_server

SHOP = Path(__file__).parent / "fixtures" / "shop"
WORKER = "src/shop/worker.py::<main>"


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    web = tmp_path_factory.mktemp("web")
    (web / "index.html").write_text("<html>viewer</html>")
    (web / "app.js").write_text("console.log(1)")
    server = make_server(load_project(SHOP), web_dir=web, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def get(url: str):
    with urllib.request.urlopen(url) as response:
        return response.headers.get_content_type(), response.read()


def get_json(url: str):
    kind, body = get(url)
    assert kind == "application/json"
    return json.loads(body)


def post_json(url: str, body: dict):
    request = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request) as response:
        assert response.headers.get_content_type() == "application/json"
        return json.loads(response.read())


def status_of(url: str) -> int:
    try:
        urllib.request.urlopen(url)
    except urllib.error.HTTPError as err:
        return err.code
    return 200


def test_project_lists_ranked_entries(base):
    data = get_json(f"{base}/api/project")
    assert data["name"] == "shop"
    assert [e["label"] for e in data["entries"]][-1] == "shop"
    assert data["entries"][0]["sources"][0].keys() == {"kind", "text"}
    assert "src/shop/worker.py" in data["files"]


@pytest.fixture(scope="module")
def map_base(tmp_path_factory):
    root = tmp_path_factory.mktemp("generic")
    (root / "src").mkdir()
    (root / "src/app.ts").write_text("import '../value';\nexport const app = 1")
    (root / "value.ts").write_text("export const value = 1")
    (root / "notes ü.custom").write_text("\n".join(str(i) for i in range(400)))
    (root / "image.png").write_bytes(b"\x89PNG\0")
    (root / "comma,folder").mkdir()
    (root / "comma,folder/readme.custom").write_text("notes")
    server = make_server(load_project(root), web_dir=root, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", root
    server.shutdown()
    server.server_close()


def test_generic_overview_summary_and_search_routes(map_base):
    url, _ = map_base
    summary = get_json(f"{url}/api/project")
    assert summary["stats"]["files"] == 0 and summary["entries"] == []
    assert summary["inventory"]["files"] == 5
    assert summary["capabilities"]["overview"] is True
    assert summary["capabilities"]["simulation_languages"] == []
    graph = get_json(f"{url}/api/overview")
    assert graph["root"] == "@project" and graph["overview"]["total_files"] == 5
    assert any(n["id"] == "dir:src" and not n["detail"]["container"] for n in graph["nodes"])
    opened = get_json(f"{url}/api/overview?expand=dir:src")
    assert any(n["id"] == "file:src/app.ts" and n["parent"] == "dir:src" for n in opened["nodes"])
    focused = get_json(f"{url}/api/overview?path=src/app.ts")
    assert focused["overview"]["files"] == 1
    assert focused["edges"][0]["confidence"] == "inferred"
    hits = get_json(f"{url}/api/map-search?" + urlencode({"q": "notes ü"}))
    assert hits == [{"path": "notes ü.custom", "label": "notes ü.custom", "category": "document", "language": "unknown"}]
    assert get_json(f"{url}/api/map-search?q=") == []


def test_map_source_bounds_and_rejects_uninventoried_or_binary_files(map_base):
    url, _ = map_base
    source = get_json(f"{url}/api/map-source?" + urlencode({"file": "notes ü.custom", "start": 4, "end": 400}))
    assert source["start"] == 4 and source["lines"] == [str(i) for i in range(3, 203)]
    for file in ("../../etc/passwd", "/etc/passwd", "missing", "image.png"):
        assert status_of(f"{url}/api/map-source?" + urlencode({"file": file})) == 404
    assert status_of(f"{url}/api/map-source?file=value.ts&start=bad") == 400
    assert status_of(f"{url}/api/source?file=value.ts") == 404
    assert status_of(f"{url}/api/overview?path=../") == 404


def test_map_mermaid_route_does_not_require_a_function(map_base):
    url, _ = map_base
    kind, data = get(f"{url}/api/mermaid?mode=map&expand=dir:src&dir=TD")
    assert kind == "text/plain"
    assert data.decode().startswith("flowchart TD") and "subgraph" in data.decode()
    assert "-.->" in data.decode()


def test_map_repeated_open_parameters_preserve_commas_in_folder_names(map_base):
    url, _ = map_base
    query = urlencode({"open": ["dir:src", "dir:comma,folder"], "expand": "dir:ignored"}, doseq=True)
    graph = get_json(f"{url}/api/overview?" + query)
    assert any(n["id"] == "file:comma,folder/readme.custom" for n in graph["nodes"])
    assert any(n["id"] == "file:src/app.ts" for n in graph["nodes"])
    _, text = get(f"{url}/api/mermaid?mode=map&" + query)
    assert "readme.custom" in text.decode() and "app.ts" in text.decode()


def test_source_endpoints_recheck_changed_symlink_containment(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("def run():\n    pass\n")
    (root / "notes.txt").write_text("notes")
    project = load_project(root)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    for name in ("app.py", "notes.txt"):
        (root / name).unlink()
        (root / name).symlink_to(outside)
    server = make_server(project, web_dir=root, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert status_of(f"{url}/api/source?file=app.py") == 404
        assert status_of(f"{url}/api/map-source?file=notes.txt") == 404
    finally:
        server.shutdown()
        server.server_close()


def test_flow_with_an_opened_step(base):
    closed = get_json(f"{base}/api/flow?" + urlencode({"root": WORKER, "start": "python -m shop.worker"}))
    assert next(n for n in closed["nodes"] if n["kind"] == "start")["label"] == "python -m shop.worker"
    step = next(n for n in closed["nodes"] if n["label"] == "work")
    opened = get_json(f"{base}/api/flow?" + urlencode({"root": WORKER, "expand": step["id"]}))
    assert next(n for n in opened["nodes"] if n["id"] == step["id"])["kind"] == "group"
    assert any(n["parent"] == step["id"] for n in opened["nodes"])


def test_unknown_function_is_not_found(base):
    assert status_of(f"{base}/api/flow?root=nope") == 404


def test_source_lines_of_a_project_file(base):
    data = get_json(f"{base}/api/source?file=src/shop/worker.py&start=5&end=7")
    assert data["lines"] == ["def work():", "    store = default_store()", "    for order in store.load_all():"]


def test_source_is_limited_to_analysed_files(base):
    for bad in ("../../../../etc/passwd", "/etc/passwd", "pyproject.toml"):
        assert status_of(f"{base}/api/source?" + urlencode({"file": bad})) == 404


def test_mermaid_export(base):
    kind, body = get(f"{base}/api/mermaid?" + urlencode({"root": WORKER, "depth": "1"}))
    assert kind == "text/plain"
    assert body.decode().startswith("flowchart LR") and "subgraph" in body.decode()
    vertical = get(f"{base}/api/mermaid?" + urlencode({"root": WORKER, "dir": "TD"}))[1].decode()
    assert vertical.startswith("flowchart TD")


def test_viewer_files_are_served_and_contained(base):
    assert get(f"{base}/") == ("text/html", b"<html>viewer</html>")
    assert get(f"{base}/app.js")[0] == "text/javascript"
    assert status_of(f"{base}/..%2F..%2Fetc%2Fpasswd") == 404


def test_viewer_falls_back_to_a_free_port_when_the_default_is_busy(tmp_path):
    import socket

    from flowmap.server import start_viewer

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    busy = blocker.getsockname()[1]
    server, url = start_viewer(load_project(SHOP), port=busy, open_browser=False, web_dir=tmp_path)
    try:
        assert not url.endswith(f":{busy}/")
        assert get_json(url + "api/project")["name"] == "shop"
    finally:
        server.shutdown()
        server.server_close()
        blocker.close()


def test_trail_steps_view_opens_the_steps_the_data_enters(base):
    query = {"root": "src/shop/cli.py::main", "at": "s", "var": "argv", "view": "steps"}
    data = get_json(f"{base}/api/trail?" + urlencode(query))
    assert data["trail"] == {"origin": "s", "var": "argv", "truncated": False, "view": "steps"}
    parse = next(n for n in data["nodes"] if n["label"] == "parse")
    assert parse["kind"] == "group"


def test_trail_journey_is_the_default_view(base):
    data = get_json(f"{base}/api/trail?" + urlencode({"root": "src/shop/cli.py::main", "at": "s", "var": "argv"}))
    assert data["trail"]["view"] == "journey"
    frames = {n["label"] for n in data["nodes"] if n["kind"] == "file"}
    assert {"src/shop/cli.py", "src/shop/payments/gateway.py"} <= frames


def test_trail_from_a_place_without_that_data_is_not_found(base):
    assert status_of(f"{base}/api/trail?" + urlencode({"root": "src/shop/cli.py::main", "at": "c99.1", "var": "x"})) == 404


def test_mermaid_export_of_a_trail(base):
    query = {"root": "src/shop/cli.py::main", "at": "s", "var": "argv", "view": "steps"}
    kind, body = get(f"{base}/api/mermaid?" + urlencode(query))
    assert 'subgraph n1["parse · src/shop/cli.py"]' in body.decode()  # opened only on the trail
    journey = get(f"{base}/api/mermaid?" + urlencode({**query, "view": "journey"}))[1].decode()
    assert '["src/shop/payments/gateway.py"]' in journey


def test_simulation_walks_the_diagram(base):
    data = get_json(f"{base}/api/simulate?" + urlencode({"root": "src/shop/cli.py::main"}))
    assert data["status"] == "done"
    first = data["frames"][0]
    assert set(first) == {"node", "edge", "hop", "note", "stack", "changes", "outputs", "error"}
    assert first["node"] == "s" and data["frames"][-1]["node"] == "e"


def test_simulation_asks_at_unknown_decisions_and_takes_answers(base):
    root = "src/shop/orders.py::OrderService.validate"
    decision = next(n["id"] for n in get_json(f"{base}/api/flow?" + urlencode({"root": root}))["nodes"] if n["kind"] == "decision")
    asked = get_json(f"{base}/api/simulate?" + urlencode({"root": root}))
    assert asked["status"] == "choose" and asked["choice"]["node"] == decision
    answered = get_json(f"{base}/api/simulate?" + urlencode({"root": root, "choices": f"{decision}=yes"}))
    assert answered["status"] == "raised"


def test_search_finds_functions_by_name(base):
    hits = get_json(f"{base}/api/search?q=save")
    assert [h["label"] for h in hits][:2] == ["Store.save", "AuditStore.save"]  # exact name, shortest first
    assert {"id", "label", "file", "line", "doc"} <= hits[0].keys()
    assert "src/shop/util.py::slugify" in [h["id"] for h in get_json(f"{base}/api/search?q=SLUG")]
    assert get_json(f"{base}/api/search?q=") == []


def test_simulation_takes_real_data_in_a_post(base):
    body = {"root": "src/shop/cli.py::main", "argv": ["--config", "cfg.json", "anna"], "env": {"SHOP_TOKEN": "t0k"}}
    data = post_json(f"{base}/api/simulate", body)
    args = next(f for f in data["frames"] if "args" in f["stack"][-1]["vars"])["stack"][-1]["vars"]["args"]
    assert args["cls"] == "Namespace" and args["v"]["names"]["v"] == [{"t": "val", "v": "anna"}]
    assert {"status", "choice", "error", "expanded", "frames"} <= data.keys()


def test_simulation_can_start_anywhere_and_open_every_step(base):
    root = "src/shop/cli.py::main"
    step = next(n["id"] for n in get_json(f"{base}/api/flow?" + urlencode({"root": root}))["nodes"] if n["label"] == "load_config")
    data = post_json(f"{base}/api/simulate", {"root": root, "start_at": step, "inputs": {"args": {"config": "x.json"}}, "auto_open": True})
    assert data["frames"][0]["node"] == step and step in data["expanded"]


def test_bad_simulation_requests_are_refused(base):
    request = urllib.request.Request(f"{base}/api/simulate", b"{not json", {"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request)
    assert refused.value.code == 400


def test_expects_lists_what_a_start_point_needs(base):
    data = get_json(f"{base}/api/expects?" + urlencode({"root": "src/shop/cli.py::main"}))
    assert data["reached"] and set(data["vars"]) == {"argv"}
    assert data["argv"] is True and "SHOP_TOKEN" in data["env"]
