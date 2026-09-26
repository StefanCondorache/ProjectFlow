"""Which calls move data into or out of a Python program.

Returns ``(channel, direction)``: channel is file | db | net | env | console |
process, direction is in | out | inout. Matching uses the resolved dotted name
of outside calls, plus a short list of method names distinctive enough to
recognise even when the receiver's type is unknown (``frame.to_csv(...)``).
"""

from __future__ import annotations

from flowmap.ir import Call

IO = tuple[str, str]

_EXACT: dict[str, IO] = {
    "open": ("file", "?"),
    "io.open": ("file", "?"),
    "input": ("console", "in"),
    "print": ("console", "out"),
    "json.load": ("file", "in"),
    "json.dump": ("file", "out"),
    "pickle.load": ("file", "in"),
    "pickle.dump": ("file", "out"),
    "joblib.load": ("file", "in"),
    "joblib.dump": ("file", "out"),
    "numpy.load": ("file", "in"),
    "numpy.loadtxt": ("file", "in"),
    "numpy.save": ("file", "out"),
    "numpy.savez": ("file", "out"),
    "numpy.savez_compressed": ("file", "out"),
    "numpy.savetxt": ("file", "out"),
    "csv.reader": ("file", "in"),
    "csv.DictReader": ("file", "in"),
    "csv.writer": ("file", "out"),
    "csv.DictWriter": ("file", "out"),
    "os.getenv": ("env", "in"),
    "os.environ.get": ("env", "in"),
    "dotenv.load_dotenv": ("env", "in"),
    "subprocess.run": ("process", "out"),
    "subprocess.call": ("process", "out"),
    "subprocess.check_call": ("process", "out"),
    "subprocess.check_output": ("process", "in"),
    "subprocess.Popen": ("process", "out"),
    "os.system": ("process", "out"),
    "shutil.copy": ("file", "out"),
    "shutil.copy2": ("file", "out"),
    "shutil.copyfile": ("file", "out"),
    "shutil.move": ("file", "out"),
    "shutil.rmtree": ("file", "out"),
    "os.remove": ("file", "out"),
    "os.unlink": ("file", "out"),
    "os.rename": ("file", "out"),
    "os.replace": ("file", "out"),
    "sqlite3.connect": ("db", "inout"),
    "sqlalchemy.create_engine": ("db", "inout"),
    "psycopg2.connect": ("db", "inout"),
    "psycopg.connect": ("db", "inout"),
    "asyncpg.connect": ("db", "inout"),
    "pymongo.MongoClient": ("db", "inout"),
    "redis.Redis": ("db", "inout"),
    "redis.StrictRedis": ("db", "inout"),
    "redis.from_url": ("db", "inout"),
    "urllib.request.urlopen": ("net", "in"),
    "smtplib.SMTP": ("net", "out"),
}

_HTTP_MODULES = {"requests", "httpx", "aiohttp", "urllib3"}
_HTTP_IN = {"get", "head", "options"}
_HTTP_OUT = {"post", "put", "patch", "delete", "request"}
_SQL_MODULES = ("sqlite3", "sqlalchemy", "psycopg", "asyncpg", "pymysql")
_SQL_RECEIVERS = {"conn", "connection", "cursor", "cur", "session", "db", "engine"}
_REDIS_METHODS = {
    "get", "set", "mget", "mset", "hget", "hset", "hgetall", "delete", "expire",
    "xadd", "xread", "xreadgroup", "publish", "lpush", "rpush", "blpop", "brpop", "zadd", "zrange",
}

_METHODS: dict[str, IO] = {
    "read_csv": ("file", "in"),
    "read_parquet": ("file", "in"),
    "read_json": ("file", "in"),
    "read_excel": ("file", "in"),
    "read_feather": ("file", "in"),
    "read_pickle": ("file", "in"),
    "read_table": ("file", "in"),
    "read_hdf": ("file", "in"),
    "read_sql": ("db", "in"),
    "read_sql_query": ("db", "in"),
    "read_sql_table": ("db", "in"),
    "to_csv": ("file", "out"),
    "to_parquet": ("file", "out"),
    "to_excel": ("file", "out"),
    "to_feather": ("file", "out"),
    "to_pickle": ("file", "out"),
    "to_hdf": ("file", "out"),
    "to_sql": ("db", "out"),
    "read_text": ("file", "in"),
    "read_bytes": ("file", "in"),
    "write_text": ("file", "out"),
    "write_bytes": ("file", "out"),
    "savefig": ("file", "out"),
    "fetchall": ("db", "in"),
    "fetchone": ("db", "in"),
    "fetchmany": ("db", "in"),
    "executemany": ("db", "out"),
}


def classify(call: Call) -> IO | None:
    if call.kind in ("project", "class"):
        return None
    method = call.callee.rsplit(".", 1)[-1] if call.callee else ""
    name = call.external if call.kind in ("external", "builtin") else None
    if name:
        found = _EXACT.get(name)
        if found is not None:
            return ("file", _open_direction(call)) if found[1] == "?" else found
        head, last = name.split(".", 1)[0], name.rsplit(".", 1)[-1].lower()
        if head in _HTTP_MODULES:
            if last in _HTTP_IN:
                return ("net", "in")
            if last in _HTTP_OUT:
                return ("net", "out")
        if head == "redis" and method in _REDIS_METHODS:
            return ("db", "inout")
        if method in ("execute", "executescript") and any(m in name for m in _SQL_MODULES):
            return ("db", "inout")
    if call.kind in ("external", "unresolved"):
        if method in _METHODS:
            return _METHODS[method]
        parts = call.callee.split(".")
        if call.kind == "unresolved" and method == "execute" and len(parts) > 1 and parts[-2] in _SQL_RECEIVERS:
            return ("db", "inout")
    return None


def _open_direction(call: Call) -> str:
    mode = ""
    positional = [a for a in call.args if a.keyword is None and not a.star]
    if len(positional) > 1:
        mode = positional[1].text
    for arg in call.args:
        if arg.keyword == "mode":
            mode = arg.text
    mode = mode.strip("'\"rbt")
    return "out" if any(ch in mode for ch in "wax+") else "in"
