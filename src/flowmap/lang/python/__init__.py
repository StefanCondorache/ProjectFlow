from flowmap.lang import Adapter
from flowmap.lang.python.entries import python_entries
from flowmap.lang.python.extract import extract_module
from flowmap.lang.python.link import link_python

ADAPTER = Adapter("python", (".py",), extract_module, link_python, python_entries)
