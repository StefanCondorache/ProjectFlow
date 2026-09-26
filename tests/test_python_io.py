import textwrap
from pathlib import Path

import pytest

from flowmap.ir import Call, walk
from flowmap.project import load_project

SHOP = Path(__file__).parent / "fixtures" / "shop"


@pytest.fixture(scope="module")
def shop():
    return load_project(SHOP)


def io_of(project, func_id: str, callee: str):
    for item in walk(project.functions[func_id].body):
        if isinstance(item, Call) and item.callee == callee:
            return item.io
    raise AssertionError(f"no call to {callee} in {func_id}")


def test_files_env_network_and_database_in_the_shop(shop):
    config = "src/shop/config.py::load_config"
    assert io_of(shop, config, "open") == ("file", "in")
    assert io_of(shop, config, "json.load") == ("file", "in")
    assert io_of(shop, config, "os.getenv") == ("env", "in")
    assert io_of(shop, "src/shop/payments/gateway.py::Gateway.charge", "requests.post") == ("net", "out")
    assert io_of(shop, "src/shop/store.py::BaseStore.connect", "sqlite3.connect") == ("db", "inout")
    assert io_of(shop, "src/shop/store.py::Store.save", "self.conn.execute") == ("db", "inout")
    assert io_of(shop, "src/shop/store.py::Store.load_all", "$1.fetchall") == ("db", "in")
    assert io_of(shop, "src/shop/util.py::log_event", "print") == ("console", "out")


def test_parsing_a_response_is_not_io(shop):
    assert io_of(shop, "src/shop/payments/gateway.py::Gateway.charge", "resp.json") is None


def test_pandas_pathlib_open_modes_and_untyped_receivers(tmp_path):
    (tmp_path / "m.py").write_text(textwrap.dedent('''
        import pandas as pd
        from pathlib import Path

        def f(path, con, frame):
            df = pd.read_csv(path)
            df.to_parquet("out.parquet")
            q = pd.read_sql("select 1", con)
            frame.to_csv("x.csv")
            Path(path).write_text("hi")
            with open(path, "w") as out:
                pass
            with open(path, mode="rb") as raw:
                pass
    '''))
    project = load_project(tmp_path)
    fid = "m.py::f"
    assert io_of(project, fid, "pd.read_csv") == ("file", "in")
    assert io_of(project, fid, "df.to_parquet") == ("file", "out")
    assert io_of(project, fid, "pd.read_sql") == ("db", "in")
    assert io_of(project, fid, "frame.to_csv") == ("file", "out")
    assert io_of(project, fid, "$1.write_text") == ("file", "out")
    opens = [c.io for c in walk(project.functions[fid].body) if isinstance(c, Call) and c.callee == "open"]
    assert opens == [("file", "out"), ("file", "in")]
