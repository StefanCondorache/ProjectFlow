"""Configuration loading."""
import json
import os


def load_config(path: str) -> dict:
    """Read settings from a JSON file."""
    with open(path) as fh:
        data = json.load(fh)
    data["token"] = os.getenv("SHOP_TOKEN")
    return data
