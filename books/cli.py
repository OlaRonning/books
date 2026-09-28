"""books: page-level full-text search over a PDF library.

  books QUERY...             search pages; pick a hit with fzf to open it
  books -t TAG --type T ...  restrict by catalog tag / type
  books -l QUERY...          print hits instead of picking
  books ls [-t TAG] [--type T]   browse the catalog
  books tags                 list tags with counts
  books stats                index coverage and catalog problems
  books add FILE...          queue PDFs (or chapter folders) in the inbox; the
                             hub merges, OCRs, identifies, names, catalogs and
                             indexes them
  books process              ingest the inbox, then index (hub only)
  books redo FILE...         re-identify catalogued PDFs from scratch (hub only)
  books index [--full]       (re)build the index (hub only)

The library is BOOKS_DIR (default ~/books); BOOKS_HUB names the one machine
allowed to write it.
"""

import argparse
import contextlib
import fcntl
import shutil
import socket
import sqlite3
import sys
from pathlib import Path

from . import config, index
from .viewer import pick_and_open


def is_hub():
    return not config.HUB or socket.gethostname() == config.HUB


@contextlib.contextmanager
def hub_lock():
    """Refuse to write off the hub, and serialise writers on it."""
    if not is_hub():
        sys.exit(f"only {config.HUB} writes the library; use `books add` to queue PDFs")
    config.CACHE.mkdir(parents=True, exist_ok=True)
    with open(config.CACHE / "lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def process():
    from . import ingest  # network, Claude, OCR: only needed when ingesting

    with hub_lock():
        ingest.process()
        index.build()


def redo(names):
    """Drop the catalog entries and send the PDFs back through ingest."""
    from . import ingest

    names = [Path(n).name for n in names]
    with hub_lock():
        removed = ingest.remove_catalog_entries(config.catalog_path(), set(names))
        for name in names:
            if name not in removed:
                print(f"not in catalog: {name}", file=sys.stderr)
                continue
            config.inbox_path().mkdir(parents=True, exist_ok=True)
            shutil.move(config.pdfs_path() / name, config.inbox_path() / name)
            print(f"re-queued {name}")
    process()


def add(paths, move):
    """Queue PDFs and chapter folders in the inbox; on the hub, ingest now."""
    inbox = config.inbox_path()
    inbox.mkdir(parents=True, exist_ok=True)
    for p in map(Path, paths):
        if p.is_dir() and any(p.glob("*.pdf")):
            (shutil.move if move else shutil.copytree)(p, inbox / p.name)
        elif p.is_file() and p.suffix.lower() == ".pdf":
            (shutil.move if move else shutil.copy2)(p, inbox / p.name)
        else:
            print(f"skip (not a PDF or folder of PDFs): {p}", file=sys.stderr)
            continue
        print(f"queued {p.name}")
    if is_hub():
        process()
    else:
        print(f"{config.HUB} will ingest it once it syncs")


def search(words, limit, raw, pick, tags, kind):
    hi, lo = ("\033[1;33m", "\033[0m") if pick or sys.stdout.isatty() else ("", "")
    try:
        rows = index.hits(words, limit, raw, tags, kind, hi, lo)
    except sqlite3.OperationalError as err:
        sys.exit(f"bad query: {err}")
    if not rows:
        sys.exit("no matches")
    lines = [f"{path}\t{page}\t{lab:<60.60}  p.{page:<5} {snip}" for path, page, lab, snip in rows]
    show(lines, pick, " ".join(words))


def list_works(tags, kind, pick):
    rows = index.works(tags, kind)
    if not rows:
        sys.exit("no works match")
    lines = [f"{f}\t1\t{typ:<8} {lab:<80.80}  [{', '.join(tag_list)}]"
             for f, typ, lab, tag_list in rows]
    show(lines, pick, "books")


def show(lines, pick, prompt):
    if pick:
        pick_and_open(lines, prompt)
    else:
        print("\n".join(line.split("\t", 2)[2] for line in lines))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else ""

    if cmd == "index":
        p = argparse.ArgumentParser(prog="books index")
        p.add_argument("--full", action="store_true", help="rebuild from scratch")
        full = p.parse_args(argv[1:]).full
        with hub_lock():
            index.build(full)
        return
    if cmd == "process":
        process()
        return
    if cmd == "redo":
        p = argparse.ArgumentParser(prog="books redo")
        p.add_argument("names", nargs="+", metavar="FILE", help="PDF file name in pdfs/")
        redo(p.parse_args(argv[1:]).names)
        return
    if cmd == "add":
        p = argparse.ArgumentParser(prog="books add")
        p.add_argument("--move", action="store_true", help="move instead of copy")
        p.add_argument("paths", nargs="+", metavar="PATH")
        a = p.parse_args(argv[1:])
        add(a.paths, a.move)
        return
    if cmd == "stats":
        index.stats()
        return
    if cmd == "tags":
        for tag, n in index.tag_counts():
            print(f"{n:4}  {tag}")
        return

    p = argparse.ArgumentParser(prog="books", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-t", "--tag", action="append", default=[], help="only works with TAG (repeatable)")
    p.add_argument("--type", choices=["book", "article", "notes"], help="only works of this type")
    p.add_argument("-l", "--list", action="store_true", help="print, don't pick with fzf")
    if cmd == "ls":
        a = p.parse_args(argv[1:])
        list_works(a.tag, a.type, not a.list and sys.stdout.isatty())
        return
    p.add_argument("-n", type=int, default=50, help="max hits (default 50)")
    p.add_argument("--raw", action="store_true", help="pass FTS5 query syntax through")
    p.add_argument("query", nargs="+")
    a = p.parse_args(argv)
    search(a.query, a.n, a.raw, not a.list and sys.stdout.isatty(), a.tag, a.type)
