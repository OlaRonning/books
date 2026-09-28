"""catalog.toml: one [[work]] per PDF in pdfs/ (`file` is the name within it)."""

import sys
import tomllib

from . import config


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
