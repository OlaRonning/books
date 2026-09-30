"""Reading on the reMarkable, through its cloud (rmapi).

  push   upload a catalogued PDF to the tablet folder, named after its file.
         If an earlier reading was pulled back, its archive goes up instead,
         so the old annotations return with it.
  pull   bring a document back (hub only): keep the tablet's own archive as
         annotations/<name>.rmdoc (lossless: the PDF, every stroke and
         highlight), render annotated/<name>.pdf, write the highlighted text
         to annotated/<name>.md, then delete it from the tablet.

The original in pdfs/ is never touched. Tablet documents are matched to
catalog entries by name, so renaming one on the tablet unlinks it.
"""

import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from . import catalog, config
from .viewer import pick

PAIR_HINT = ("pair rmapi once: run `rmapi` and enter the code from "
             "https://my.remarkable.com/device/browser/connect")


def rmapi(*args, cwd=None, check=True):
    """Run rmapi non-interactively (it never prompts for a pairing code)."""
    if not shutil.which("rmapi"):
        sys.exit("rmapi not found")
    r = subprocess.run(["rmapi", "-ni", *args], cwd=cwd, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, check=False)
    if check and r.returncode != 0:
        err = (r.stderr or r.stdout).strip().splitlines()
        sys.exit(f"rmapi {args[0]} failed: {err[-1] if err else r.returncode}\n({PAIR_HINT} if you haven't)")
    return r


def glob_escape(name):
    """rmapi's rm matches its last path element as a glob; make it literal."""
    return "".join("\\" + c if c in "*?[]\\" else c for c in name)


def folder_path():
    return "/" + config.TABLET_FOLDER.strip("/")


def tablet_docs():
    """Names of the documents in the tablet folder (empty if there is none)."""
    folder = folder_path()
    parent, name = os.path.split(folder)
    listing = json.loads(rmapi("-json", "ls", parent).stdout or "[]")
    if not any(n["name"] == name and n["type"] == "CollectionType" for n in listing):
        return set()
    return {n["name"] for n in json.loads(rmapi("-json", "ls", folder).stdout or "[]")
            if n["type"] == "DocumentType"}


def choose(names, words, prompt, label=lambda n: n):
    """The one name `words` pick out: an exact name, the single name containing
    all the words, or a pick with fzf among several."""
    names = sorted(names)
    query = " ".join(words)
    stem = Path(query).name.removesuffix(".pdf")
    for n in names:
        if stem and Path(n).name.removesuffix(".pdf") == stem:
            return n
    hits = [n for n in names if all(w.lower() in label(n).lower() for w in words)]
    if not hits:
        sys.exit(f"nothing matches: {query}" if words else "nothing to choose from")
    if len(hits) == 1:
        return hits[0]
    if not sys.stdout.isatty():
        sys.exit("several match:\n  " + "\n  ".join(hits))
    chosen = pick([f"{n}\t1\t{label(n)}" for n in hits], prompt)
    if not chosen:
        sys.exit(1)
    return chosen[0]


# --- push / pull / list --------------------------------------------------------

def push(words):
    works = catalog.load()
    file = choose(works, words, "push", lambda f: catalog.label(works[f]))
    name = Path(file).stem
    folder = folder_path()
    if name in tablet_docs():
        sys.exit(f"already on the tablet: {folder}/{name}")
    archive = config.annotations_path() / f"{name}.rmdoc"
    rmapi("mkdir", folder)  # says "entry already exists" when it does
    rmapi("put", str(archive if archive.exists() else config.pdfs_path() / file), folder)
    if name not in tablet_docs():
        sys.exit(f"upload of {name} did not arrive in {folder}")
    print(f"pushed {name}" + (" with its earlier annotations" if archive.exists() else ""))


def pull(words, keep=False):
    """Archive a tablet document in the library; the caller holds the hub lock."""
    works = catalog.load()
    folder = folder_path()
    name = choose(tablet_docs(), words, "pull")
    file = f"{name}.pdf"
    if file not in works:
        sys.exit(f"{folder}/{name} is not a library PDF (renamed on the tablet?); leaving it there")

    with tempfile.TemporaryDirectory() as tmp:
        # geta fetches the whole document (the same archive `get` saves as
        # .rmdoc) and renders it; a failed render still leaves the archive.
        r = rmapi("geta", "-a", f"{folder}/{name}", cwd=tmp, check=False)
        archive, rendered = Path(tmp, f"{name}.zip"), Path(tmp, f"{name}-annotations.pdf")
        problem = check_archive(archive)
        if problem:
            detail = (r.stderr or r.stdout).strip()
            sys.exit(f"download of {name} failed ({problem}); left on the tablet\n{detail}")
        install(archive, config.annotations_path() / f"{name}.rmdoc")
        notes = highlights_markdown(highlights(archive), works[file])
        if notes:
            write_text(config.annotated_path() / f"{name}.md", notes)
        else:
            (config.annotated_path() / f"{name}.md").unlink(missing_ok=True)
        if rendered.exists():
            install(rendered, config.annotated_path() / file)
            warn_page_shift(file)
        else:
            print(f"rendering failed; annotations are safe in annotations/{name}.rmdoc",
                  file=sys.stderr)
    print(f"archived annotations/{name}.rmdoc"
          + (f", highlights in annotated/{name}.md" if notes else ""))

    if keep:
        return
    rmapi("rm", f"{folder}/{glob_escape(name)}")
    if name in tablet_docs():
        sys.exit(f"could not delete {folder}/{name} from the tablet")
    print(f"removed {name} from the tablet")


def list_tablet():
    works = catalog.load()
    for name in sorted(tablet_docs(), key=str.lower):
        note = "" if f"{name}.pdf" in works else "   (not in the library)"
        print(f"{name}{note}")


# --- archives ----------------------------------------------------------------

def check_archive(path):
    """None if `path` is a complete document archive, else what is wrong."""
    if not path.exists():
        return "no archive"
    try:
        with zipfile.ZipFile(path) as z:
            if z.testzip() is not None:
                return "corrupt archive"
            names = z.namelist()
    except zipfile.BadZipFile:
        return "not a zip archive"
    if not any(n.endswith(".content") for n in names):
        return "archive without .content"
    if not any(n.endswith(".pdf") for n in names):
        return "archive without the PDF"
    return None


def install(src, dest):
    """Copy into the library and replace `dest` in one step (Syncthing never
    ships half a file)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)


def write_text(dest, text):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, dest)


def page_count(pdf):
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=False).stdout
    return next((int(line.split()[1]) for line in out.splitlines() if line.startswith("Pages:")), None)


def warn_page_shift(file):
    """Search hits carry page numbers of the original; say when the annotated
    copy (which `books` opens instead) numbers them differently."""
    a, b = page_count(config.pdfs_path() / file), page_count(config.annotated_path() / file)
    if a and b and a != b:
        print(f"note: annotated copy has {b} pages, the original {a} (pages added on the tablet?); "
              "search hits may open a few pages off", file=sys.stderr)


def page_map(content):
    """(page id, 1-based PDF page or None for a page added on the tablet),
    in document order, from a .content file (firmware 3+ cPages, or older)."""
    cpages = content.get("cPages")
    if cpages:
        def value(field):
            return field.get("value") if isinstance(field, dict) else field
        pages = [p for p in cpages.get("pages", []) if not value(p.get("deleted"))]
        pages.sort(key=lambda p: value(p.get("idx")) or "")
        redirs = [value(p.get("redir")) for p in pages]
        return [(p["id"], r + 1 if r is not None else None) for p, r in zip(pages, redirs)]
    ids = content.get("pages", [])
    redir = content.get("redirectionPageMap") or list(range(len(ids)))
    return [(pid, r + 1 if r >= 0 else None) for pid, r in zip(ids, redir)]


# Highlighter colours by RGBA (newer firmware stores the colour this way).
RGBA_NAMES = {
    (255, 237, 117): "yellow", (190, 234, 254): "blue", (242, 158, 255): "pink",
    (255, 195, 140): "orange", (172, 255, 133): "green", (199, 199, 198): "gray",
}


def color_name(glyph):
    if glyph.color_rgba:
        r, g, b = glyph.color_rgba[:3]
        return RGBA_NAMES.get((r, g, b), f"#{r:02x}{g:02x}{b:02x}")
    return glyph.color.name.lower().removeprefix("highlight_").removesuffix("_2").replace("_", " ")


def merge_glyphs(glyphs):
    """[(text, colour)]: ranges of one highlight that runs over several lines
    arrive separately, a line break apart, and are joined back up."""
    out, prev = [], None
    for g in sorted(glyphs, key=lambda g: (g.start is None, g.start or 0)):
        color = color_name(g)
        if (prev is not None and out and out[-1][1] == color and g.start is not None
                and prev.start is not None and 0 <= g.start - (prev.start + prev.length) <= 2):
            out[-1] = (out[-1][0] + " " + g.text.strip(), color)
        else:
            out.append((g.text.strip(), color))
        prev = g
    return [(" ".join(text.split()), color) for text, color in out if text.strip()]


def highlights(archive):
    """[(PDF page, [(text, colour)])] in page order, from a document archive."""
    from rmscene import BlockOverflowError, UnexpectedBlockError, read_tree  # hub only
    from rmscene import scene_items as si

    out = []
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
        content = next(n for n in names if n.endswith(".content"))
        doc = content.removesuffix(".content")
        for page_id, pdf_page in page_map(json.loads(z.read(content))):
            member = f"{doc}/{page_id}.rm"
            if pdf_page is None or member not in names:
                continue
            try:
                tree = read_tree(io.BytesIO(z.read(member)))
            except (ValueError, EOFError, struct.error, UnexpectedBlockError, BlockOverflowError) as err:
                # an older .rm version, or a newer one rmscene cannot read yet
                print(f"skip highlights on p. {pdf_page}: {err}", file=sys.stderr)
                continue
            found = merge_glyphs(g for g in tree.walk() if isinstance(g, si.GlyphRange))
            if found:
                out.append((pdf_page, found))
    return out


def highlights_markdown(pages, work):
    if not pages:
        return ""
    pulled = datetime.now().astimezone().date().isoformat()
    lines = [f"# {catalog.label(work)}: highlights", "",
             f"Pulled from the reMarkable on {pulled}. Page numbers are PDF pages.", ""]
    for page, found in pages:
        lines.append(f"## p. {page}")
        lines.extend(f"- {text}" + ("" if color == "yellow" else f" *({color})*") for text, color in found)
        lines.append("")
    return "\n".join(lines)
