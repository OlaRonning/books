"""rofi script mode: `rofi -modi books:books-rofi -show books`.

rofi runs this with no argument for the initial rows, then again with the
chosen row (ROFI_RETV=1) or with typed text that matched no row (ROFI_RETV=2).
Rows carry 'file<TAB>page' in their info field; choosing one opens it.
Typed text is a full-text search; a leading 't:TAG' word narrows it to a tag.
"""

import os
import sqlite3
import sys

from . import index
from .viewer import open_pdf


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def row(text, path, page):
    print(f"{text}\0info\x1f{path}\t{page}")


def header(prompt, message):
    print(f"\0prompt\x1f{prompt}")
    print(f"\0message\x1f{message}")
    print("\0markup-rows\x1ftrue")
    print("\0no-custom\x1ffalse")


def main():
    retv = os.environ.get("ROFI_RETV", "0")
    info = os.environ.get("ROFI_INFO", "")
    typed = " ".join(sys.argv[1:]).strip()

    if retv == "1" and info:  # picked a work or a hit
        path, page = info.split("\t")
        open_pdf(path, page)
        return  # printing no rows makes rofi close

    if retv == "2" and typed:  # typed text that matched no row: search
        words = typed.split()
        tags = [w[2:] for w in words if w.startswith("t:")]
        words = [w for w in words if not w.startswith("t:")]
        header("search", "Enter opens the page · type a new query + Enter to search again")
        if not words:
            for f, _, lab, tag_list in index.works(tags):
                row(f"{esc(lab)}  <small>{esc(', '.join(tag_list))}</small>", f, 1)
            return
        try:
            found = index.hits(words, 100, tags=tags, hi="\x02", lo="\x03")
        except sqlite3.OperationalError as err:
            row(esc(f"bad query: {err}"), "", 0)
            return
        if not found:
            print(f"\0message\x1fno matches for {esc(typed)} · type a new query + Enter")
        for path, page, lab, snip in found:
            snip = esc(snip).replace("\x02", "<b>").replace("\x03", "</b>")
            row(f"<b>{esc(lab)}</b>  p.{page}  <small>{snip}</small>", path, page)
        return

    header("books", "Type to filter works · Enter on unmatched text searches every page"
                    " · <i>t:TAG query</i> narrows by tag")
    for f, typ, lab, tag_list in index.works():
        row(f"{esc(lab)}  <small>{esc(typ)} · {esc(', '.join(tag_list))}</small>", f, 1)
