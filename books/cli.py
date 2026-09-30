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
  books review               work through flagged entries, parked duplicates
                             and failed ingests (any machine)
  books dupes [--backfill]   list suspected duplicates; --backfill first adds
                             verified DOI/arXiv/ISBN fields to entries (hub only)
  books index [--full]       (re)build the index (hub only)
  books push [QUERY...]      send a catalogued PDF to the reMarkable (with its
                             annotations, if an earlier reading was pulled)
  books pull [--keep] [QUERY...]
                             bring a document back from the reMarkable: archive
                             it with its annotations, render an annotated copy,
                             extract highlights, delete it there (hub only)
  books tablet               list the documents in the reMarkable folder

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

from . import catalog, config, index
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
    names = [Path(n).name for n in names]
    with hub_lock():
        removed = catalog.remove_entries(set(names))
        for name in names:
            if name not in removed:
                print(f"not in catalog: {name}", file=sys.stderr)
                continue
            config.inbox_path().mkdir(parents=True, exist_ok=True)
            shutil.move(config.pdfs_path() / name, config.inbox_path() / name)
            print(f"re-queued {name}")
    process()


def find_dupes(backfill):
    from . import dupes, ingest

    if backfill:
        with hub_lock():
            added = ingest.backfill_ids(catalog.load())
            print(f"identifiers added to {len(added)} entries")
            index.build()
    works = catalog.load()
    shelf = config.pdfs_path()
    hashes = {f: dupes.sha256(shelf / f) for f in works if (shelf / f).exists()}
    pairs = dupes.report(works, hashes)
    for a, b, reason in pairs:
        print(f"{reason}:\n  {a}\n  {b}")
    parked = sorted((config.inbox_path() / "duplicates").glob("*.pdf"))
    if parked:
        print(f"parked in inbox/duplicates/: {', '.join(p.name for p in parked)}")
    if not (pairs or parked):
        print(f"no duplicates among {len(works)} works")


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
    if cmd == "review":
        from . import review

        review.main()
        return
    if cmd == "dupes":
        p = argparse.ArgumentParser(prog="books dupes")
        p.add_argument("--backfill", action="store_true",
                       help="first add verified identifiers to catalog entries (hub only)")
        find_dupes(p.parse_args(argv[1:]).backfill)
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
    if cmd in ("push", "pull", "tablet"):
        from . import remarkable

        p = argparse.ArgumentParser(prog=f"books {cmd}")
        if cmd == "pull":
            p.add_argument("--keep", action="store_true", help="leave it on the tablet too")
        if cmd != "tablet":
            p.add_argument("query", nargs="*", help="file name or words from the title (default: pick)")
        a = p.parse_args(argv[1:])
        if cmd == "push":
            remarkable.push(a.query)
        elif cmd == "pull":
            with hub_lock():
                remarkable.pull(a.query, a.keep)
        else:
            remarkable.list_tablet()
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
