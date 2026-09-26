from flowmap.entries import find_commands


def argvs(root):
    return [c.argv for c in find_commands(root)]


def test_shell_variables_and_subshells_are_expanded(tmp_path):
    (tmp_path / "cycle.sh").write_text(
        'REPO="$(cd "$(dirname "$0")" && pwd)"\n'
        'MODULE="pkg.job"\n'
        "OTHER=pkg.other\n"
        '(cd "$REPO" && .venv/bin/python3 -m "$MODULE") 2>&1 | tee log\n'
        'python -m ${OTHER} --day "$(date +%F)"\n'
        "python -m ${MISSING:-pkg.fallback}\n"
    )
    found = argvs(tmp_path)
    assert [".venv/bin/python3", "-m", "pkg.job"] in found
    assert ["python", "-m", "pkg.other", "--day", "X"] in found
    assert ["python", "-m", "pkg.fallback"] in found


def test_dockerfile_shell_form_with_line_continuation(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM x\nCMD python \\\n    app.py --port 80\n")
    commands = find_commands(tmp_path)
    assert [(c.argv, c.where) for c in commands] == [(["python", "app.py", "--port", "80"], "Dockerfile:2")]


def test_procfile_processes(tmp_path):
    (tmp_path / "Procfile").write_text("web: gunicorn app:app\nworker: python worker.py\n")
    assert [(c.argv, c.where) for c in find_commands(tmp_path)] == [
        (["gunicorn", "app:app"], "Procfile: web"),
        (["python", "worker.py"], "Procfile: worker"),
    ]
