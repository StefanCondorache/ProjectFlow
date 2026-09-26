"""Language adapters.

An adapter turns the source files of one language into the shared IR
(``flowmap.ir``) and resolves the calls between them. Adding a language means
adding an adapter; everything downstream is shared.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from flowmap.entries import Command, Entry
from flowmap.ir import Module


@dataclass(frozen=True)
class Adapter:
    language: str
    extensions: tuple[str, ...]
    extract: Callable[[str, bytes], Module]
    link: Callable[[Path, dict[str, Module]], None]
    entries: Callable[[Path, dict[str, Module], list[Command]], list[Entry]]


def adapters() -> list[Adapter]:
    from flowmap.lang.python import ADAPTER as python

    return [python]
