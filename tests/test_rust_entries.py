"""Rust entries: binary crate roots, examples and literal cargo commands."""
from pathlib import Path

from flowmap.project import load_project

MAIN = 'fn main() { run(); }\nfn run() {}\n'


def write(root: Path, files: dict[str, str]):
    for relative, source in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)


def summary(project):
    return [(e.label, e.kind, e.target.split('::')[0]) for e in project.entries]


def test_binaries_examples_and_excluded_targets(tmp_path):
    write(tmp_path, {
        'Cargo.toml': '[package]\nname = "shop-cli"\n\n[[bin]]\nname = "admin"\npath = "tools/admin.rs"\n',
        'src/main.rs': MAIN, 'src/bin/report.rs': MAIN, 'src/bin/sync/main.rs': MAIN, 'tools/admin.rs': MAIN,
        'examples/demo.rs': MAIN, 'tests/it.rs': MAIN, 'benches/b.rs': MAIN, 'build.rs': MAIN,
        'src/lib.rs': 'pub fn main() {}',
    })
    entries = summary(load_project(tmp_path))
    assert sorted(entries) == sorted([
        ('cargo run --bin shop-cli', 'cargo-bin', 'src/main.rs'),
        ('cargo run --bin report', 'cargo-bin', 'src/bin/report.rs'),
        ('cargo run --bin sync', 'cargo-bin', 'src/bin/sync/main.rs'),
        ('cargo run --bin admin', 'cargo-bin', 'tools/admin.rs'),
        ('cargo run --example demo', 'cargo-example', 'examples/demo.rs'),
    ])
    assert [e[1] for e in entries][-1] == 'cargo-example'


def test_cargo_run_commands_attach_to_their_binary(tmp_path):
    write(tmp_path, {
        'Cargo.toml': '[package]\nname = "shop"\n',
        'src/main.rs': MAIN, 'src/bin/report.rs': MAIN,
        'run.sh': 'cargo run --release --bin report -- --verbose\ncargo run\n',
    })
    sources = {e.target.split('::')[0]: [s.text for s in e.sources] for e in load_project(tmp_path).entries}
    # Declared Cargo binaries outrank shell scripts; the commands are kept as evidence.
    assert 'run.sh:1' in sources['src/bin/report.rs'] and 'run.sh:2' in sources['src/main.rs']
    assert 'run.sh:1' not in sources['src/main.rs']


def test_entry_target_is_the_root_main_function(tmp_path):
    write(tmp_path, {'Cargo.toml': '[package]\nname = "shop"\n',
                     'src/main.rs': 'mod inner { pub fn main() {} }\nfn main() { inner::main(); }\n'})
    entry = load_project(tmp_path).entries[0]
    assert entry.target.startswith('src/main.rs::main@2:') and entry.reach == 1
