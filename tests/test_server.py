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
