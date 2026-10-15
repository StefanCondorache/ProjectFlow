"""JavaScript and TypeScript adapters sharing extraction and linking."""

from flowmap.lang import Adapter
from flowmap.lang.ecmascript.entries import ecmascript_entries
from flowmap.lang.ecmascript.extract import extract_module
from flowmap.lang.ecmascript.link import link_ecmascript

ADAPTERS = (
    Adapter("javascript", (".js", ".jsx", ".mjs", ".cjs"), extract_module,
            link_ecmascript, ecmascript_entries, simulation=False, family="ecmascript"),
    Adapter("typescript", (".ts", ".tsx", ".mts", ".cts"), extract_module,
            link_ecmascript, ecmascript_entries, simulation=False, family="ecmascript"),
)
