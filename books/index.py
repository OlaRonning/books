"""The page-level full-text index (SQLite FTS5).

One row per PDF page: `body` holds the page text, `meta` the work's authors,
title and tags from the catalog. Results rank by BM25 with metadata weighted
5x. The index lives in the library (so a sync tool can carry it to readers),
is built in the cache directory, and is moved into place in one step.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from . import catalog, config

# Bump when SCHEMA changes: indexes built with another version are rebuilt.
SCHEMA_VERSION = "3"
SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    path TEXT PRIMARY KEY, mtime REAL, size INTEGER, pages INTEGER, entry TEXT
);
CREATE TABLE IF NOT EXISTS works (
    file TEXT PRIMARY KEY, type TEXT, label TEXT, tags TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
    meta, body, path UNINDEXED, page UNINDEXED,
    tokenize = 'porter unicode61'
);
"""


def extract(pdf):
    """The text of each page (pdftotext separates pages with a form feed)."""
    try:
        out = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", str(pdf), "-"],
            capture_output=True, check=True, timeout=600,
        ).stdout.decode("utf-8", "replace")
    except (subprocess.SubprocessError, OSError) as err:
        print(f"skip {pdf.name}: {err}", file=sys.stderr)
        return None
    pages = out.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def entry_hash(work):
    """Fingerprint of a catalog entry; a page's metadata is stale when it changes."""
    return hashlib.sha256(json.dumps(work, sort_keys=True).encode()).hexdigest()


def work_rows(works):
    return sorted((f, w.get("type", ""), catalog.label(w), "," + ",".join(w.get("tags", [])) + ",")
                  for f, w in works.items())


def build(full=False):
    """Bring the index up to date: re-extract only PDFs whose file or catalog
    entry changed. A no-op (no file written) when nothing changed."""
    works = catalog.load()
    index = config.index_path()

    config.CACHE.mkdir(parents=True, exist_ok=True)
    tmp = config.CACHE / "index.sqlite.tmp"
    tmp.unlink(missing_ok=True)
    if index.exists() and not full:
        shutil.copy2(index, tmp)

    db = sqlite3.connect(tmp)
    try:
        db.executescript(SCHEMA)
        meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.DatabaseError:
        meta = {}  # unreadable or foreign schema
    if meta.get("schema") != SCHEMA_VERSION:
        db.close()
        tmp.unlink()
        db = sqlite3.connect(tmp)
        db.executescript(SCHEMA)

    known = {p: (m, s, e) for p, m, s, e in db.execute("SELECT path, mtime, size, entry FROM docs")}
    current = {name: (*sig, entry_hash(works.get(name, {})))
               for name, sig in catalog.library_pdfs().items()}
    gone = known.keys() - current.keys()
    todo = [p for p, sig in current.items() if known.get(p) != sig]
    rows = work_rows(works)
    works_changed = rows != sorted(db.execute("SELECT file, type, label, tags FROM works"))
    if not (todo or gone or works_changed):
        db.close()
        tmp.unlink()
        print(f"index up to date ({len(current)} PDFs)")
        catalog.report(works, current)
        return

    db.execute("DELETE FROM works")
    db.executemany("INSERT INTO works VALUES (?, ?, ?, ?)", rows)
    for name in gone | set(todo):
        db.execute("DELETE FROM pages WHERE path = ?", (name,))
        db.execute("DELETE FROM docs WHERE path = ?", (name,))

    with ThreadPoolExecutor(os.cpu_count()) as pool:
        texts = pool.map(lambda name: extract(config.pdfs_path() / name), todo)
        for name, pages in zip(todo, texts):
            if pages is None:
                continue
            w = works.get(name, {})
            meta = " ".join([*w.get("authors", []), w.get("title", ""), *w.get("tags", [])])
            db.executemany(
                "INSERT INTO pages (meta, body, path, page) VALUES (?, ?, ?, ?)",
                ((meta, text, name, i) for i, text in enumerate(pages, 1)),
            )
            mtime, size, entry = current[name]
            db.execute("INSERT INTO docs VALUES (?, ?, ?, ?, ?)", (name, mtime, size, len(pages), entry))
            print(f"indexed {name} ({len(pages)} pages)")

    db.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
    db.commit()
    db.execute("INSERT INTO pages(pages) VALUES ('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    shutil.move(tmp, index)
    print(f"{len(todo)} updated, {len(gone)} removed, {len(current)} PDFs in index")
    catalog.report(works, current)


def connect():
    index = config.index_path()
    if not index.exists():
        hub = config.HUB or "the hub"
        sys.exit(f"no index at {index}; run `books index` on {hub}")
    return sqlite3.connect(f"file:{index}?mode=ro", uri=True)


def work_filter(tags, kind):
    """SQL condition on works (alias w) and its parameters, for tag/type filters."""
    conds, params = [], []
    for tag in tags:
        conds.append("w.tags LIKE ?")
        params.append(f"%,{tag},%")
    if kind:
        conds.append("w.type = ?")
        params.append(kind)
    return (" AND ".join(conds) or "1"), params


def fts_query(words, raw=False):
    """Quote each word so '-', ':' etc. are literal; all words must match."""
    if raw:
        return " ".join(words)
    return " ".join('"' + w.replace('"', '""') + '"' for w in " ".join(words).split())


def hits(words, limit=50, raw=False, tags=(), kind=None, hi="", lo=""):
    """Ranked page hits as (file, page, label, snippet); raises on bad FTS syntax."""
    cond, params = work_filter(tags, kind)
    rows = connect().execute(
        # FTS5 functions need the unaliased table, so match in a subquery
        # and join the catalog onto its results.
        f"""SELECT h.path, h.page, coalesce(w.label, h.path), h.snip
            FROM (SELECT path, page, bm25(pages, 5.0, 1.0) AS rank,
                         snippet(pages, -1, '{hi}', '{lo}', '…', 14) AS snip
                  FROM pages WHERE pages MATCH ?) h
            LEFT JOIN works w ON w.file = h.path
            WHERE {cond}
            ORDER BY h.rank LIMIT ?""",
        (fts_query(words, raw), *params, limit),
    ).fetchall()
    return [(path, page, lab, " ".join(snip.split())) for path, page, lab, snip in rows]


def works(tags=(), kind=None):
    """Catalogued works as (file, type, label, tag list), sorted by label."""
    cond, params = work_filter(tags, kind)
    rows = connect().execute(
        f"SELECT w.file, w.type, w.label, w.tags FROM works w WHERE {cond} ORDER BY w.label",
        params,
    ).fetchall()
    return [(f, typ, lab, [t for t in tag_str.split(",") if t]) for f, typ, lab, tag_str in rows]


def tag_counts():
    counts = {}
    for (tag_str,) in connect().execute("SELECT tags FROM works"):
        for t in filter(None, tag_str.split(",")):
            counts[t] = counts.get(t, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def stats():
    db = connect()
    docs, pages = db.execute("SELECT count(*), sum(pages) FROM docs").fetchone()
    by_type = db.execute("SELECT type, count(*) FROM works GROUP BY type ORDER BY 2 DESC").fetchall()
    empty = db.execute(
        "SELECT path FROM docs WHERE path NOT IN "
        "(SELECT DISTINCT path FROM pages WHERE length(trim(body)) > 50)"
    ).fetchall()
    index = config.index_path()
    print(f"{index} ({index.stat().st_size / 1e6:.0f} MB)")
    print(f"{docs} PDFs, {pages or 0} pages; works: "
          + ", ".join(f"{n} {t or 'untyped'}" for t, n in by_type))
    if empty:
        print("no extractable text (scanned?):")
        for (path,) in empty:
            print(f"  {path}")
    catalog.report(catalog.load(), catalog.library_pdfs())
