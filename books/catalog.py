"""catalog.toml: one [[work]] per PDF in pdfs/ (`file` is the name within it).

Reads parse the whole file with tomllib. Writes edit it block by block, so
comments, ordering and formatting of untouched entries survive, and every
write replaces the file atomically (a sync tool never ships half a catalog).
"""

import json
import os
import re
import sys
import tomllib

from . import config

BLOCK = re.compile(r"(?m)^(?=\[\[work\]\]\s*$)")


def load():
    """{file name: work dict} from catalog.toml (empty if there is none)."""
    path = config.catalog_path()
    if not path.exists():
        return {}
    with open(path, "rb") as fh:
        return {w["file"]: w for w in tomllib.load(fh).get("work", [])}


def label(work):
    """'Author (Year) Title', reusing the short author form from the file name."""
    short = work["file"].split(" - ", 1)[0]
    year = f" ({work['year']})" if "year" in work else ""
    return f"{short}{year} {work['title']}"


def library_pdfs():
    """{file name: (mtime, size)} for the PDFs in pdfs/."""
    return {p.name: (p.stat().st_mtime, p.stat().st_size)
            for p in config.pdfs_path().glob("*.pdf")}


def report(works, pdfs):
    """Warn about PDFs without a catalog entry and entries without a PDF."""
    for name in sorted(pdfs.keys() - works.keys()):
        print(f"not in catalog: {name}", file=sys.stderr)
    for name in sorted(works.keys() - pdfs.keys()):
        print(f"catalog entry without file: {name}", file=sys.stderr)


# --- writing -----------------------------------------------------------------

def toml_str(s):
    return json.dumps(s, ensure_ascii=False)  # JSON strings are valid TOML basic strings


def entry_text(file, work):
    """A [[work]] block for a new entry."""
    lines = ["", "[[work]]", f"file = {toml_str(file)}", f"type = {toml_str(work.get('type', 'book'))}"]
    lines.append("authors = [" + ", ".join(toml_str(a) for a in work.get("authors", [])) + "]")
    if work.get("editors"):
        lines.append("editors = true")
    lines.append(f"title = {toml_str(work['title'])}")
    if work.get("year"):
        lines.append(f"year = {int(work['year'])}")
    for key in ("edition", "venue", "doi", "arxiv", "isbn"):
        if work.get(key):
            lines.append(f"{key} = {toml_str(str(work[key]))}")
    lines.append("tags = [" + ", ".join(toml_str(t) for t in work.get("tags", [])) + "]")
    if work.get("review"):
        # A string saying why (older entries: true). Cleared by `books review`.
        reason = work["review"] if isinstance(work["review"], str) else "identified without a lookup"
        lines.append(f"review = {toml_str(reason)}")
    return "\n".join(lines) + "\n"


def _blocks(path):
    head, *blocks = BLOCK.split(path.read_text())
    return head, [(tomllib.loads(b)["work"][0].get("file"), b) for b in blocks]


def _write(path, text):
    tmp = path.with_name(".catalog.toml.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _gap(block):
    """Trailing blank lines of a block, kept when the block is rewritten."""
    return block[len(block.rstrip("\n")):] or "\n"


def append(entry, path=None):
    path = path or config.catalog_path()
    text = path.read_text() if path.exists() else ""
    _write(path, text.rstrip("\n") + "\n" + entry)


def entry_block(file, path=None):
    """The [[work]] block text for `file` (for editing), or None."""
    _, blocks = _blocks(path or config.catalog_path())
    return next((b.rstrip("\n") + "\n" for name, b in blocks if name == file), None)


def remove_entries(names, path=None):
    """Drop the blocks for these file names; returns the names removed."""
    path = path or config.catalog_path()
    head, blocks = _blocks(path)
    removed = [name for name, _ in blocks if name in names]
    if removed:
        _write(path, head + "".join(b for name, b in blocks if name not in names))
    return removed


def update_entries(updates, path=None):
    """Add fields to existing blocks: updates is {file: {key: value}}. Keys a
    block already has are left alone. Returns the files updated."""
    path = path or config.catalog_path()
    head, blocks = _blocks(path)
    out, done = [head], []
    for name, block in blocks:
        entry = tomllib.loads(block)["work"][0]
        new = {k: v for k, v in updates.get(name, {}).items() if k not in entry}
        if new:
            block = (block.rstrip("\n") + "\n"
                     + "\n".join(f"{k} = {toml_str(str(v))}" for k, v in new.items()) + _gap(block))
            done.append(name)
        out.append(block)
    if done:
        _write(path, "".join(out))
    return done


def remove_key(file, key, path=None):
    """Delete `key = ...` lines from the block for `file`; True if it had one."""
    path = path or config.catalog_path()
    head, blocks = _blocks(path)
    out, changed = [head], False
    for name, block in blocks:
        if name == file:
            kept = re.sub(rf"(?m)^{re.escape(key)}\s*=.*\n?", "", block)
            changed = kept != block
            block = kept
        out.append(block)
    if changed:
        _write(path, "".join(out))
    return changed


def parse_entry(text):
    """Validate an edited block: exactly one [[work]] with file and title."""
    works = tomllib.loads(text).get("work", [])
    if len(works) != 1 or not works[0].get("file") or not works[0].get("title"):
        raise ValueError("expected exactly one [[work]] with `file` and `title`")
    return works[0]


def replace_entry(file, text, path=None):
    """Replace the block for `file` with `text` (validated with parse_entry)."""
    parse_entry(text)
    path = path or config.catalog_path()
    head, blocks = _blocks(path)
    if file not in (name for name, _ in blocks):
        raise KeyError(file)
    _write(path, head + "".join(text.rstrip("\n") + "\n" + _gap(b) if name == file else b
                                for name, b in blocks))
