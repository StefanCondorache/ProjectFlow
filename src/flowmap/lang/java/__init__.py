"""Java adapter: parser-backed extraction, conservative type-based linking."""

from flowmap.lang import Adapter
from flowmap.lang.java.entries import java_entries
from flowmap.lang.java.extract import extract_module
from flowmap.lang.java.link import link_java

ADAPTERS = (
    Adapter("java", (".java",), extract_module, link_java, java_entries, simulation=False, family="java"),
)
