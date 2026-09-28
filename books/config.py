"""Locations and roles, from the environment (the Nix wrapper sets defaults).

BOOKS_DIR  library root (default ~/books)
BOOKS_HUB  hostname of the one machine that writes library, catalog and index;
           unset means any machine may write (single-machine setups)
"""

import os
from pathlib import Path

LIBRARY = Path(os.environ.get("BOOKS_DIR", Path.home() / "books"))
HUB = os.environ.get("BOOKS_HUB", "")
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "books"


def index_path():
    return LIBRARY / ".books-index.sqlite"


def catalog_path():
    return LIBRARY / "catalog.toml"


def inbox_path():
    return LIBRARY / "inbox"
