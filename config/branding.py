"""Shared BMS icon for the workspace and the separate participation host."""

from base64 import b64encode
from functools import cache
from pathlib import Path


FAVICON_PATH = Path(__file__).with_name("favicon.png")


@cache
def favicon_data_url():
    return "data:image/png;base64," + b64encode(FAVICON_PATH.read_bytes()).decode("ascii")
