import textwrap

from flowmap.flow import build_flow
from flowmap.mermaid import to_mermaid
from flowmap.project import load_project


def mermaid_lines(tmp_path, src: str, **kw) -> list[str]:
    (tmp_path / "m.py").write_text(textwrap.dedent(src))
    text = to_mermaid(build_flow(load_project(tmp_path), "m.py::main", **kw))
    assert text.startswith("flowchart TD\n")
    return [line.strip() for line in text.splitlines()]


def test_shapes_and_labelled_edges(tmp_path):
    lines = mermaid_lines(tmp_path, '''
        def a(): pass
        def main(flag):
            if flag:
                a()
    ''')
    # nodes are numbered in diagram order: start, decision, a, end
    assert 'n0(["main"])' in lines
    assert 'n1{"flag"}' in lines
    assert 'n2["a<br/>m.py"]' in lines
    assert 'n3(["END"])' in lines
    assert 'n0 -->|"flag"| n1' in lines
    assert 'n1 -->|"yes"| n2' in lines
    assert 'n1 -->|"no"| n3' in lines
    assert "n2 --> n3" in lines


def test_frames_become_subgraphs_and_labels_are_escaped(tmp_path):
    lines = mermaid_lines(tmp_path, '''
        def process(it): pass
        def main(items):
            for it in items:
                if it != "<x>":
                    process(it)
    ''')
    start = lines.index('subgraph n1["for it in items"]')
    assert lines[start + 1] == "direction TB"
    assert 'n2{"it != #quot;#lt;x#gt;#quot;"}' in lines[start:]
    assert "end" in lines[start:]


def test_error_paths_are_dotted(tmp_path):
    lines = mermaid_lines(tmp_path, '''
        def risky(): pass
        def recover(): pass
        def main():
            try:
                risky()
            except ValueError:
                recover()
    ''')
    assert any(line.startswith("n1 -.->") and "ValueError" in line for line in lines)
