"""Browse every inventoried child independently of the bounded map projection."""

from __future__ import annotations

import posixpath
from pathlib import PurePosixPath

from flowmap.inventory import Inventory


def browse_inventory(inventory: Inventory, path: str = "", offset: int = 0, limit: int = 40) -> dict:
    """Return a folders-first page; a focused file browses its parent folder."""
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")

    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise FileNotFoundError(f"unknown project path: {path!r}")
    folder = relative.as_posix()
    if folder == ".":
        folder = ""
    if folder in inventory.files:
        folder = posixpath.dirname(folder)
    elif folder and folder not in inventory.directories:
        raise FileNotFoundError(f"unknown project path: {path!r}")

    items = [
        {"path": directory, "label": posixpath.basename(directory), "category": "folder", "language": ""}
        for directory in inventory.directories
        if posixpath.dirname(directory) == folder
    ]
    items.extend(
        {"path": file, "label": posixpath.basename(file), "category": item.category, "language": item.language}
        for file, item in inventory.files.items()
        if posixpath.dirname(file) == folder
    )
    items.sort(key=lambda item: (item["category"] != "folder", item["label"], item["path"]))
    return {"path": folder, "items": items[offset:offset + limit], "total": len(items), "offset": offset, "limit": limit}
