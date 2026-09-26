"""What the calls a simulated program makes to the world outside it do.

Nothing here touches that world. Files inside the project are really read
(hidden ones never, big ones not); whatever is written, printed or logged is
only recorded, as an output of the walk; the environment and the command line
are what the walk was given, and argparse parses that command line for real.
"""

from __future__ import annotations

import argparse
import builtins
import csv
import io
import json
import posixpath
import re
import tomllib
from pathlib import Path, PurePath, PurePosixPath
from typing import TYPE_CHECKING

import yaml

from flowmap.ir import Call, self_name
from flowmap.lang.python.evaluate import (
    BUILTIN_TYPES,
    LIBRARY,
    NOT_PURE,
    EvalError,
    call_builtin,
    call_library,
    call_method,
    is_known,
    lineage_of,
    render,
)
from flowmap.values import Handle, Obj, Parser, Unknown, encode, exception, is_exception, summary

if TYPE_CHECKING:
    from flowmap.simulate import _Scope

MAX_FILE = 2_000_000  # bytes: larger project files are not read
MAX_OUTPUTS = 200  # outputs kept per frame
MAX_LISTED = 200_000  # names of a folder listing


def _text(value) -> str:
    """How print shows a value; what cannot be known reads like the code."""
    if isinstance(value, str):
        return value
    if is_exception(value):
        message = value.fields.get("message", "")
        return message if isinstance(message, str) else summary(message)
    if isinstance(value, (Unknown, Obj, Handle, Parser)) or not is_known(value):
        return summary(value)
    return str(value)


class _ArgExit(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


class _Argparse(argparse.ArgumentParser):
    """A real parser that reports instead of printing and exiting."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.printed: list[str] = []

    def exit(self, status=0, message=None):
        raise _ArgExit(status, message or "")

    def error(self, message):
        raise _ArgExit(2, f"{self.format_usage()}{self.prog}: error: {message}\n")

    def _print_message(self, message, file=None):
        if message:
            self.printed.append(message)


_LOG_LEVELS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
_LOGGER = re.compile(r"(?:^|\.)(?:log|logger|_log|_logger|LOG|LOGGER|logging)\.(\w+)$")
_PATH_DISK = {
    "read_text", "read_bytes", "exists", "is_file", "is_dir", "open", "write_text", "write_bytes", "mkdir",
    "touch", "unlink", "rmdir", "rename", "replace", "iterdir", "glob", "rglob", "resolve", "absolute",
    "expanduser", "stat",
}
_CHANGERS = {
    "append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse", "update", "setdefault",
    "add", "discard", "popitem", "appendleft", "extendleft", "__setitem__",
}

_WORLD = {
    "open": "open_file",
    "io.open": "open_file",
    "json.load": "load_json",
    "yaml.safe_load": "load_yaml",
    "yaml.load": "load_yaml",
    "yaml.full_load": "load_yaml",
    "tomllib.load": "load_toml",
    "tomli.load": "load_toml",
    "csv.reader": "csv_rows",
    "csv.DictReader": "csv_rows",
    "json.dump": "dump",
    "yaml.dump": "dump",
    "yaml.safe_dump": "dump",
    "pickle.dump": "dump",
    "print": "print_",
    "os.getenv": "getenv",
    "os.environ.get": "getenv",
    "os.path.exists": "exists",
    "os.path.isfile": "isfile",
    "os.path.isdir": "isdir",
    "os.listdir": "listdir",
    "sys.exit": "exit_",
    "exit": "exit_",
    "quit": "exit_",
    "argparse.ArgumentParser": "parser",
}


class Outside:
    """The part of the walk's runner that answers calls to the outside world."""

    # the world outside the project

    def outside(self, call: Call, receiver, args: list, kwargs: dict, scope: _Scope):
        name = call.external or (call.callee if call.kind == "builtin" else "")
        method = call.method

        if isinstance(receiver, Handle):
            return self.handle_method(receiver, method, args, call)
        if isinstance(receiver, Parser):
            return self.parser_method(receiver, method, args, kwargs, call, scope)
        if isinstance(receiver, PurePath) and method in _PATH_DISK:
            return self.path_method(receiver, method, args, kwargs, call, scope)
        if is_exception(receiver) and method == "__init__":
            receiver.fields["message"] = self.message(call, args, scope)
            return None
        if isinstance(receiver, Unknown) and method == "get" and args and is_known(args[0]):
            try:
                if args[0] in receiver.known:
                    return receiver.known[args[0]]
            except TypeError:
                pass

        world = _WORLD.get(name)
        if world is not None:
            return getattr(self, world)(call, args, kwargs, scope)
        if call.kind == "builtin" and "." not in call.callee:
            special = self.builtin(call, args, kwargs, scope)
            if special is not NOT_PURE:
                return special
        level = self.log_level(call, name)
        if level:
            self.log(level, call, args, scope)
            return None

        if call.io:
            if call.io[1] in ("out", "inout"):
                target = self.path_text(args[0]) if args and call.io[0] == "file" else None
                data = args[0] if args else (kwargs.get("json") or kwargs.get("data"))
                self.record(target or call.io[0], name or call.callee, data)
            return Unknown(call.text)

        try:
            if call.kind == "builtin" and "." not in call.callee:
                result = call_builtin(call.callee, args, kwargs)
            elif name in LIBRARY:
                result = call_library(name, args, kwargs)
            elif receiver is not None and method:
                result = call_method(receiver, method, args, kwargs)
            else:
                result = NOT_PURE
        except EvalError as err:
            raise self.fail(err.type_name, err.message, err.bases or None) from None
        if result is NOT_PURE or isinstance(result, Unknown):
            if method in _CHANGERS and call.receiver:
                self.spoil(call.receiver, scope, call.text)  # something in it changed, somewhere
            return Unknown(call.text)  # in the words of the code
        return result

    def builtin(self, call: Call, args: list, kwargs: dict, scope: _Scope):
        name = call.callee
        if name == "super":
            owner = self_name(scope.fn)
            return scope.vars.get(owner, Unknown("super()")) if owner else Unknown("super()")
        exc_type = getattr(builtins, name, None)
        if isinstance(exc_type, type) and issubclass(exc_type, BaseException):
            if issubclass(exc_type, KeyError) and len(args) == 1 and is_known(args[0]):
                return exception(name, repr(args[0]))  # as str(KeyError(...)) shows it
            return exception(name, self.message(call, args, scope))
        if name == "isinstance" and len(args) == 2 and isinstance(args[0], Obj):
            names = {n.rsplit(".", 1)[-1] for n in re.findall(r"[A-Za-z_][\w.]*", call.args[1].text)} if len(call.args) > 1 else set()
            return bool(names & {args[0].cls, *args[0].bases})
        if name in ("getattr", "hasattr") and len(args) >= 2 and isinstance(args[0], Obj) and isinstance(args[1], str):
            if args[1] in args[0].fields:
                return True if name == "hasattr" else args[0].fields[args[1]]
            if name == "getattr" and len(args) == 3:
                return args[2]
            return Unknown(call.text)
        if name == "setattr" and len(args) == 3 and isinstance(args[1], str):
            if isinstance(args[0], Obj):
                args[0].fields[args[1]] = args[2]
            elif isinstance(args[0], Unknown):
                args[0].known[args[1]] = args[2]
            return None
        if name == "str" and len(args) == 1 and is_exception(args[0]):
            return args[0].fields.get("message", "")
        if name == "next" and args and isinstance(args[0], list):
            if args[0]:
                return args[0].pop(0)  # an iterator made from a list
            if len(args) > 1:
                return args[1]
            raise self.fail("StopIteration")
        if name == "iter" and len(args) == 1 and isinstance(args[0], (list, tuple, str, dict)):
            return list(args[0])
        return NOT_PURE

    def log_level(self, call: Call, name: str) -> str | None:
        if name.startswith("logging.") and name.rsplit(".", 1)[-1] in _LOG_LEVELS:
            return name.rsplit(".", 1)[-1]
        found = _LOGGER.search(call.callee)
        if found and found.group(1) in _LOG_LEVELS and call.kind != "project":
            return found.group(1)
        return None

    def log(self, level: str, call: Call, args: list, scope: _Scope) -> None:
        texts = self.readable(call, args, scope)
        if level == "log" and args:
            args, texts = args[1:], texts[1:]
        if not args:
            return
        text = texts[0]
        if len(args) > 1 and isinstance(args[0], str) and "%" in args[0]:
            try:
                plain = [a if is_known(a) and not isinstance(a, (Obj, Handle, Parser)) else t for a, t in zip(args[1:], texts[1:])]
                text = args[0] % tuple(plain)
            except (TypeError, ValueError):
                pass
        self.record("log", level, text)

    def readable(self, call: Call, args: list, scope: _Scope) -> list[str]:
        """How each positional argument reads in a message: f-strings are
        filled in as far as they can be."""
        positional = [a for a in call.args if not a.keyword and not a.star]
        out = []
        for index, value in enumerate(args):
            text = None
            if isinstance(value, Unknown) and len(positional) == len(args):
                text = render(positional[index].expr, self.lookup(scope))
            out.append(text if text is not None else _text(value))
        return out

    def message(self, call: Call, args: list, scope: _Scope) -> str:
        if not args:
            return ""
        texts = self.readable(call, args, scope)
        return texts[0] if len(texts) == 1 else "(" + ", ".join(texts) + ")"

    def record(self, target: str, how: str, data) -> None:
        if self.recording and len(self.pending) < MAX_OUTPUTS:
            self.pending.append({"target": target, "how": how, "data": encode(data)})

    # files: real reads inside the project, writes only recorded

    @staticmethod
    def path_text(value) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, PurePath):
            return str(value)
        return None

    def project_file(self, name: str, scope: _Scope, want: str = "file") -> Path | None:
        """The real path for ``name`` inside the project, or None. Hidden
        files and folders (``.env``, ``.git``) are never opened."""
        root = self.project.root
        given = Path(name)
        options = [given] if given.is_absolute() else [root / name, root / posixpath.dirname(scope.fn.file) / name]
        for option in options:
            try:
                resolved = option.resolve()
            except (OSError, RuntimeError):
                continue
            if resolved != root and not resolved.is_relative_to(root):
                continue
            if any(part.startswith(".") for part in resolved.relative_to(root).parts):
                continue
            if want == "file" and resolved.is_file() and resolved.stat().st_size <= MAX_FILE:
                return resolved
            if want == "dir" and resolved.is_dir():
                return resolved
            if want == "any" and resolved.exists():
                return resolved
        return None

    def read(self, handle: Handle) -> str | None:
        if handle.path is None:
            return None
        if handle.path not in self.texts:
            try:
                self.texts[handle.path] = handle.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                self.texts[handle.path] = None
        return self.texts[handle.path]

    def inside(self, name: str) -> bool:
        """Whether a path is the project's to answer about."""
        given = Path(name)
        if not given.is_absolute():
            return True
        try:
            return given.resolve().is_relative_to(self.project.root)
        except (OSError, RuntimeError):
            return False

    def open_file(self, call: Call, args: list, kwargs: dict, scope: _Scope):
        name = self.path_text(args[0] if args else kwargs.get("file"))
        mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")
        mode = mode if isinstance(mode, str) else "r"
        if name is None:
            return Unknown(call.text)
        if any(flag in mode for flag in "wax+"):
            return Handle(name, None, mode)
        path = self.project_file(name, scope)
        if path is None:  # elsewhere, hidden, or made at run time: only a run would know
            return Unknown(call.text)
        return Handle(name, path, mode)

    def parse(self, loader, text: str):
        try:
            return loader(text)
        except Exception as err:  # the file's real error, e.g. JSONDecodeError
            raise self.fail(type(err).__name__, str(err), lineage_of(type(err))) from None

    def source_text(self, value) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, Handle) and not value.writing:
            return self.read(value)
        return None

    def load_json(self, call: Call, args: list, kwargs: dict, scope: _Scope):
        text = self.source_text(args[0]) if args and isinstance(args[0], Handle) else None
        return self.parse(json.loads, text) if text is not None else Unknown(call.text)

    def load_yaml(self, call: Call, args: list, kwargs: dict, scope: _Scope):
        text = self.source_text(args[0] if args else kwargs.get("stream"))
        return self.parse(yaml.safe_load, text) if text is not None else Unknown(call.text)

    def load_toml(self, call: Call, args: list, kwargs: dict, scope: _Scope):
        text = self.source_text(args[0]) if args else None
        return self.parse(tomllib.loads, text) if text is not None else Unknown(call.text)

    def csv_rows(self, call: Call, args: list, kwargs: dict, scope: _Scope):
        text = self.source_text(args[0]) if args and isinstance(args[0], Handle) else None
        options = {k: v for k, v in kwargs.items() if k in ("delimiter", "quotechar") and isinstance(v, str)}
        if text is None:
            return Unknown(call.text)
        if call.external == "csv.DictReader":
            return self.parse(lambda t: list(csv.DictReader(io.StringIO(t), **options)), text)
        return self.parse(lambda t: list(csv.reader(io.StringIO(t), **options)), text)

    def dump(self, call: Call, args: list, kwargs: dict, scope: _Scope):
        target = args[1] if len(args) > 1 else kwargs.get("fp") or kwargs.get("stream")
        if target is None:  # yaml.dump(data) with no stream returns the text
            return Unknown(call.text)
        name = target.name if isinstance(target, Handle) else self.path_text(target) or summary(target)
        self.record(name, call.external or call.callee, args[0] if args else None)
        return None

    def print_(self, call: Call, args: list, kwargs: dict, scope: _Scope):
        sep = kwargs.get("sep", " ")
        text = (sep if isinstance(sep, str) else " ").join(self.readable(call, args, scope))
        target = kwargs.get("file")
        self.record(target.name if isinstance(target, Handle) else "console", "print", text)
        return None

    def getenv(self, call: Call, args: list, kwargs: dict, scope: _Scope):
        key = args[0] if args else kwargs.get("key")
        default = args[1] if len(args) > 1 else kwargs.get("default")
        if isinstance(key, str) and self.env is not None and key in self.env:
            value = self.env[key]
            return default if value is None else value
        return Unknown(call.text)

    def exists(self, call: Call, args: list, kwargs: dict, scope: _Scope):
        return self.disk_check(args[0] if args else None, "any", call, scope)

    def isfile(self, call: Call, args: list, kwargs: dict, scope: _Scope):
        return self.disk_check(args[0] if args else None, "file", call, scope)

    def isdir(self, call: Call, args: list, kwargs: dict, scope: _Scope):
        return self.disk_check(args[0] if args else None, "dir", call, scope)

    def disk_check(self, value, want: str, call: Call, scope: _Scope):
        name = self.path_text(value)
        if name is None or not self.inside(name):
            return Unknown(call.text)
        return self.project_file(name, scope, want) is not None

    def listdir(self, call: Call, args: list, kwargs: dict, scope: _Scope):
        name = self.path_text(args[0]) if args else "."
        folder = self.project_file(name, scope, "dir") if name is not None and self.inside(name) else None
        if folder is None:
            return Unknown(call.text)
        return sorted(child.name for child in folder.iterdir())[:MAX_LISTED]

    def exit_(self, call: Call, args: list, kwargs: dict, scope: _Scope):
        code = args[0] if args else None
        message = code if isinstance(code, str) else f"exit code {code if code is not None else 0}"
        raise self.fail("SystemExit", message, code=code if not isinstance(code, str) else 1)

    def handle_method(self, handle: Handle, method: str, args: list, call: Call):
        if method == "read":
            text = self.read(handle)
            return text if text is not None else Unknown(call.text)
        if method == "readlines":
            text = self.read(handle)
            return text.splitlines(keepends=True) if text is not None else Unknown(call.text)
        if method in ("write", "writelines"):
            self.record(handle.name, method, args[0] if args else None)
            return None
        if method in ("close", "flush"):
            return None
        if method == "__enter__":
            return handle
        return Unknown(call.text)

    def path_method(self, path: PurePath, method: str, args: list, kwargs: dict, call: Call, scope: _Scope):
        name = str(path)
        if method == "read_text":
            real = self.project_file(name, scope)
            return self.read(Handle(name, real)) if real is not None else Unknown(call.text)
        if method in ("exists", "is_file", "is_dir"):
            return self.disk_check(name, {"exists": "any", "is_file": "file", "is_dir": "dir"}[method], call, scope)
        if method == "open":
            return self.open_file(call, [name, *args], kwargs, scope)
        if method in ("write_text", "write_bytes"):
            self.record(name, method, args[0] if args else None)
            return None
        if method in ("mkdir", "touch", "unlink", "rmdir", "rename", "replace"):
            self.record(name, method, args[0] if args else None)
            return None
        if method in ("resolve", "absolute"):
            if path.is_absolute():
                return PurePosixPath(posixpath.normpath(name))
            return PurePosixPath(posixpath.normpath(str(self.project.root / name)))
        if method in ("iterdir", "glob", "rglob"):
            folder = self.project_file(name, scope, "dir") if self.inside(name) else None
            if folder is None:
                return Unknown(call.text)
            if method == "iterdir":
                found = folder.iterdir()
            elif args and isinstance(args[0], str):
                found = folder.glob(args[0]) if method == "glob" else folder.rglob(args[0])
            else:
                return Unknown(call.text)
            children = []
            for child in found:
                if not any(part.startswith(".") for part in child.relative_to(folder).parts):
                    children.append(path / child.relative_to(folder).as_posix())
                if len(children) >= 5000:
                    break
            return sorted(children)
        if method == "expanduser":
            return Unknown(call.text) if name.startswith("~") else path
        return Unknown(call.text)

    # the command line

    def parser(self, call: Call, args: list, kwargs: dict, scope: _Scope):
        options = {k: v for k, v in kwargs.items() if isinstance(v, (str, bool)) and k in ("prog", "description", "epilog", "add_help", "allow_abbrev")}
        options.setdefault("prog", posixpath.basename(self.root.file))
        return Parser(options)

    def parser_method(self, parser: Parser, method: str, args: list, kwargs: dict, call: Call, scope: _Scope):
        if method == "add_argument":
            parser.arguments.append((list(args), dict(kwargs)))
            return None
        if method in ("add_argument_group", "add_mutually_exclusive_group"):
            return parser
        if method == "set_defaults":
            parser.defaults.update(kwargs)
            return None
        if method == "add_subparsers":
            parser.options["subparsers"] = True
            return Unknown(call.text)
        if method in ("parse_args", "parse_known_args"):
            given = args[0] if args else kwargs.get("args")
            if given is None:
                argv = self.argv
            elif isinstance(given, (list, tuple)) and is_known(given):
                argv = [str(a) for a in given]
            else:
                argv = None
            if argv is None or parser.options.get("subparsers"):
                return Unknown(call.text)
            return self.command_line(parser, argv, method == "parse_known_args")
        if method in ("print_help", "print_usage"):
            real = self.real_parser(parser)
            self.record("console", method, real.format_help() if method == "print_help" else real.format_usage())
            return None
        if method in ("format_help", "format_usage"):
            real = self.real_parser(parser)
            return real.format_help() if method == "format_help" else real.format_usage()
        if method == "error":
            real = self.real_parser(parser)
            message = f"{real.format_usage()}{real.prog}: error: {_text(args[0]) if args else ''}\n"
            raise self.fail("SystemExit", message, code=2)
        return Unknown(call.text)

    def real_parser(self, parser: Parser) -> _Argparse:
        options = {k: v for k, v in parser.options.items() if k != "subparsers"}
        real = _Argparse(**options)
        for names, given in parser.arguments:
            if not names or not all(isinstance(n, str) for n in names):
                continue
            clean = {}
            for key, value in given.items():
                if key == "action" and isinstance(value, Unknown) and "BooleanOptionalAction" in value.origin:
                    clean[key] = argparse.BooleanOptionalAction
                elif key == "type":
                    if isinstance(value, type) and value in BUILTIN_TYPES.values():
                        clean[key] = value
                elif isinstance(value, (Unknown, Obj, Handle, Parser)) or not is_known(value):
                    continue
                else:
                    clean[key] = value
            try:
                real.add_argument(*names, **clean)
            except (ValueError, TypeError, argparse.ArgumentError):
                continue
        real.set_defaults(**{k: v for k, v in parser.defaults.items() if is_known(v)})
        return real

    def command_line(self, parser: Parser, argv: list[str], known: bool):
        real = self.real_parser(parser)
        try:
            if known:
                space, extra = real.parse_known_args(argv)
            else:
                space, extra = real.parse_args(argv), None
        except _ArgExit as stop:
            for text in real.printed:
                self.record("console", "argparse", text)
            message = stop.message.strip() or f"exit code {stop.status}"
            raise self.fail("SystemExit", message, code=stop.status) from None
        namespace = Obj("Namespace", vars(space))
        return (namespace, extra) if known else namespace
