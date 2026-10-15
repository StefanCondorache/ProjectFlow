"""Rust adapter: parser-backed extraction, crate/module-aware conservative linking."""

from flowmap.lang import Adapter
from flowmap.lang.rust.entries import rust_entries
from flowmap.lang.rust.extract import extract_module
from flowmap.lang.rust.link import link_rust

ADAPTERS = (
    Adapter("rust", (".rs",), extract_module, link_rust, rust_entries, simulation=False, family="rust"),
)
