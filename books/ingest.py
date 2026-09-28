"""Ingest new PDFs: OCR if needed, identify, rename, catalog.

Runs on the hub only (`books process`, triggered by a systemd path unit when
something lands in the library or its inbox). For each new PDF:

  0. A folder of chapter PDFs is first merged into one PDF, chapters in
     reading order, with one bookmark per chapter.
  1. OCR it (ocrmypdf --skip-text) when the first pages have no text.
  2. Identify it: a DOI, arXiv id or ISBN on the first pages is looked up
     (Crossref, arXiv, Open Library) for exact metadata. `claude -p` then
     picks tags from the existing vocabulary, and fills in authors/title/
     year/type itself when no identifier resolved.
  3. Rename to 'Author - Title (Year[, ed]).pdf' in pdfs/ and append a
     [[work]] entry to catalog.toml (review = true when unsure).
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from . import catalog, config

UA = {"User-Agent": "books-ingest/1.0 (personal PDF library)"}
PARTICLES = {"van", "von", "der", "den", "de", "da", "di", "du", "la", "le", "del"}
BOOK_PAGES = 150  # longer than this: treat as a book, prefer ISBN over DOI
BOOK_TYPES = ("book", "monograph", "edited-book", "reference-book")
ARTICLE_TYPES = ("journal-article", "proceedings-article", "posted-content", "report")


# --- text ------------------------------------------------------------------

def first_pages(pdf, n=5):
    # check=False: an unreadable PDF yields no text, which callers handle
    return subprocess.run(["pdftotext", "-l", str(n), str(pdf), "-"],
                          capture_output=True, text=True, check=False).stdout


# Download stamps (e.g. Cambridge Core: "Downloaded from ... IP address: ...")
# are noise for identification and should not be sent anywhere.
WATERMARK = re.compile(r"^.*(Downloaded from|IP address|subject to the .* terms of use|"
                       r"cambridge\.org/core/(terms|product)).*$\n?", re.MULTILINE | re.IGNORECASE)
IMPRINT = re.compile(r"ISBN|©|\(c\) ?(19|20)\d\d|First published|Copyright", re.IGNORECASE)
FRONT_PAGES = 15  # copyright pages sit after half-title, bios and series pages


def strip_watermarks(text):
    return WATERMARK.sub("", text)


def front_matter(pdf, n=FRONT_PAGES):
    """The first n pages' text, one string per page, without download stamps."""
    pages = first_pages(pdf, n).split("\f")
    return [strip_watermarks(t) for t in pages]


def claude_text(pages, n=5):
    """What Claude sees: the first n pages, plus the imprint page if it is later."""
    head = pages[:n]
    imprint = next((t for t in pages[n:] if IMPRINT.search(t)), None)
    return "\n\f".join(head + ([f"[imprint page]\n{imprint}"] if imprint else []))


def imprint_year(text):
    """Publication year from the imprint: 'First published 2021', else the
    latest year on a copyright line."""
    m = re.search(r"First published(?: in)?[^\n\d]{0,20}((?:19|20)\d\d)", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    years = [int(y) for line in text.splitlines() if re.search(r"©|copyright|\(c\)", line, re.IGNORECASE)
             for y in re.findall(r"\b((?:19|20)\d\d)\b", line)]
    return max(years) if years else None


def pick_year(work, imprint):
    """A book's own imprint ('First published 2021') describes this copy;
    catalogue years (Open Library's first_publish_year) can be another edition's."""
    if imprint and (work.get("type") == "book" or not work.get("year")):
        return imprint
    return work.get("year")


def page_count(pdf):
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True,
                         check=False).stdout  # unreadable PDF: 0 pages
    m = re.search(r"^Pages:\s+(\d+)", out, re.MULTILINE)
    return int(m.group(1)) if m else 0


def ocr_if_needed(pdf, log):
    if len("".join(strip_watermarks(first_pages(pdf)).split())) >= 200:  # stamps are not text
        return
    log(f"  no text layer; running OCR on {pdf.name}")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ocr.pdf"
        r = subprocess.run(["ocrmypdf", "--skip-text", "-l", "eng", "--jobs",
                            str(os.cpu_count()), "--output-type", "pdf", str(pdf), str(out)],
                           capture_output=True, text=True, check=False)  # rc checked below
        if r.returncode == 0 and page_count(out) == page_count(pdf):
            shutil.move(out, pdf)
        else:
            log(f"  OCR failed ({r.returncode}); keeping original")


# --- identifiers and lookups -----------------------------------------------

def find_ids(text, filename):
    ids = {}
    m = re.search(r"arXiv:\s*(\d{4}\.\d{4,5})", text) or re.match(r"(\d{4}\.\d{4,5})(v\d+)?\.pdf$", filename)
    if m:
        ids["arxiv"] = m.group(1)
    m = re.search(r"\b(10\.\d{4,9}/[^\s\"<>]+)", text)
    if m:
        ids["doi"] = m.group(1).rstrip(".,;)")
    isbns = []
    for cand in re.findall(r"ISBN(?:-1[03])?:?\s*([\d\- ]{10,17}[\dXx])", text):
        digits = re.sub(r"[^\dXx]", "", cand).upper()
        if valid_isbn(digits) and digits not in isbns:
            isbns.append(digits)
    if isbns:
        ids["isbn"] = isbns
    return ids


def valid_isbn(s):
    if len(s) == 13 and s.isdigit():
        return sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(s)) % 10 == 0
    if len(s) == 10 and s[:9].isdigit():
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(s))
        return total % 11 == 0
    return False


def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        return r.read()


def lookup_arxiv(aid):
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = ET.fromstring(fetch(f"https://export.arxiv.org/api/query?id_list={aid}")).find("a:entry", ns)
    if entry is None:
        return None
    title = entry.findtext("a:title", "", ns)
    published = entry.findtext("a:published", "", ns)
    return {
        "type": "article",
        "title": " ".join(title.split()),
        "authors": [a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)],
        "year": int(published[:4]) if published[:4].isdigit() else None,
        "venue": f"arXiv:{aid}",
    } if title else None


def lookup_crossref(doi):
    msg = json.loads(fetch("https://api.crossref.org/works/" + urllib.parse.quote(doi)))["message"]
    people = msg.get("author") or msg.get("editor") or []
    parts = (msg.get("issued") or msg.get("published") or {}).get("date-parts", [[None]])[0]
    kind = msg.get("type", "")
    container = (msg.get("container-title") or [""])[0]
    if kind not in ARTICLE_TYPES and kind not in BOOK_TYPES and container:
        # A chapter (Cambridge tags these "other"): catalogue the book instead.
        book = lookup_isbn(msg.get("ISBN", [])) if msg.get("ISBN") else None
        if book:
            return book
        editors = msg.get("editor") or []
        return {"type": "book", "title": container, "year": parts[0],
                "authors": [" ".join(filter(None, [p.get("given"), p.get("family")]))
                            for p in editors or people],
                "editors": bool(editors)} if (editors or people) else None
    work = {
        "type": "book" if kind in BOOK_TYPES else "article",
        "title": ": ".join(filter(None, [(msg.get("title") or [""])[0], (msg.get("subtitle") or [""])[0]])),
        "authors": [" ".join(filter(None, [p.get("given"), p.get("family")])) for p in people],
        "year": parts[0],
        "crossref_type": kind,
    }
    if not msg.get("author") and msg.get("editor"):
        work["editors"] = True
    venue = (msg.get("container-title") or [""])[0]
    if venue and work["type"] == "article":
        vol = msg.get("volume")
        work["venue"] = f"{venue} {vol}" if vol else venue
    return work if work["title"] else None


def lookup_isbn(isbns):
    for isbn in isbns:
        docs = json.loads(fetch(
            f"https://openlibrary.org/search.json?isbn={isbn}"
            "&fields=title,subtitle,author_name,first_publish_year"))["docs"]
        if docs and docs[0].get("title") and docs[0].get("author_name"):
            d = docs[0]
            return {
                "type": "book",
                "title": ": ".join(filter(None, [d["title"], d.get("subtitle")])),
                "authors": d["author_name"],
                "year": d.get("first_publish_year"),
            }
    return None


def lookup(ids, pages, log, is_book=False):
    """Try identifiers in the order that best fits the document's length."""
    is_book = is_book or pages > BOOK_PAGES
    order = ["isbn", "doi", "arxiv"] if is_book else ["arxiv", "doi", "isbn"]
    fns = {"arxiv": lookup_arxiv, "doi": lookup_crossref, "isbn": lookup_isbn}
    for key in order:
        if key not in ids:
            continue
        try:
            work = fns[key](ids[key])
        except (OSError, ValueError, KeyError, ET.ParseError) as err:
            # network (URLError is an OSError), JSON/XML parse, missing fields:
            # fall through to the next identifier
            log(f"  {key} lookup failed: {err}")
            continue
        # A long PDF whose DOI is a chapter/article DOI is not that article.
        if work and key == "doi" and is_book and work["type"] != "book":
            log(f"  ignoring DOI {ids[key]} ({work.get('crossref_type')}) for a {pages}-page PDF")
            continue
        if work and work.get("title") and work.get("authors"):
            work.pop("crossref_type", None)
            log(f"  identified via {key} {ids[key]}")
            return work
    return None


# --- Claude ----------------------------------------------------------------

PROMPT = """You are cataloguing a PDF for a personal research library.

Return ONLY a JSON object with these keys:
  type     "book", "article" or "notes"
  authors  list of full names, as printed (for edited volumes, the editors)
  editors  true if the names are editors of an edited volume, else false
  title    full title, including subtitle after a colon
  year     integer year of this edition/version, or null
  edition  e.g. "2nd", "draft", or null for a first edition
  venue    journal/conference/series for articles, else null
  tags     2-5 lowercase hyphenated tags: first a broad subject, then specifics

Prefer tags from this existing vocabulary; add a new tag only if none fits:
{vocab}

{known}Filename: {filename}
Pages: {pages}

First pages of the PDF:
---
{text}
---"""


def ask_claude(text, filename, pages, vocab, known, log):
    known_txt = ""
    if known:
        known_txt = ("Bibliographic metadata was looked up from an identifier; keep "
                     "authors, title and year exactly as given unless clearly wrong:\n"
                     + json.dumps(known, ensure_ascii=False) + "\n\n")
    prompt = PROMPT.format(vocab=", ".join(sorted(vocab)) or "(none yet)", known=known_txt,
                           filename=filename, pages=pages, text=text[:15000])
    try:
        r = subprocess.run(
            ["claude", "-p", "--model", "sonnet", "--output-format", "json",
             "--disallowedTools", "Bash Edit Write WebFetch WebSearch"],
            input=prompt, capture_output=True, text=True, timeout=300,
            check=True,
        )
        reply = json.loads(r.stdout)["result"]
        body = reply[reply.index("{"): reply.rindex("}") + 1]
        return json.loads(body)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as err:
        # not installed / not logged in / timed out / non-JSON reply
        log(f"  claude failed: {err}")
        return None


# --- naming and catalog ----------------------------------------------------

def surname(name):
    if "," in name:
        return name.split(",")[0].strip()
    toks = name.split()
    for i, t in enumerate(toks[1:], 1):
        if t.lower() in PARTICLES:
            return " ".join(toks[i:])
    return toks[-1] if toks else "Unknown"


def short_authors(work):
    names = [surname(a) for a in work.get("authors", [])]
    if not names:
        short = "Unknown"
    elif len(names) == 1:
        short = names[0]
    elif len(names) == 2:
        short = f"{names[0]} & {names[1]}"
    else:
        short = f"{names[0]} et al."
    return short + (" (eds)" if work.get("editors") else "")


def clean(s):
    s = s.replace(": ", ", ").replace(":", " ").replace("/", "-").replace("\\", "-")
    s = re.sub(r'[<>"|?*\x00-\x1f]', "", s)
    return " ".join(s.split())


def file_name(work, shelf):
    title = clean(work["title"])
    if len(title) > 120:
        title = title[:120].rsplit(" ", 1)[0]
    bits = [str(work["year"])] if work.get("year") else []
    ed = work.get("edition")
    if ed:
        bits.append(ed if "draft" in ed else f"{ed} ed")
    suffix = f" ({', '.join(bits)})" if bits else ""
    base = f"{clean(short_authors(work))} - {title}{suffix}"
    name, n = f"{base}.pdf", 2
    while (shelf / name).exists():
        name, n = f"{base} [{n}].pdf", n + 1
    return name


def toml_str(s):
    return json.dumps(s, ensure_ascii=False)


def catalog_entry(file, work):
    lines = ["", "[[work]]", f"file = {toml_str(file)}", f"type = {toml_str(work.get('type', 'book'))}"]
    lines.append("authors = [" + ", ".join(toml_str(a) for a in work.get("authors", [])) + "]")
    if work.get("editors"):
        lines.append("editors = true")
    lines.append(f"title = {toml_str(work['title'])}")
    if work.get("year"):
        lines.append(f"year = {int(work['year'])}")
    for key in ("edition", "venue"):
        if work.get(key):
            lines.append(f"{key} = {toml_str(work[key])}")
    lines.append("tags = [" + ", ".join(toml_str(t) for t in work.get("tags", [])) + "]")
    if work.get("review"):
        lines.append("review = true  # identified without a lookup; check me")
    return "\n".join(lines) + "\n"


def remove_catalog_entries(catalog, names):
    """Drop the [[work]] blocks for these file names; returns the names removed."""
    text = catalog.read_text()
    head, *blocks = re.split(r"(?m)^(?=\[\[work\]\]\s*$)", text)
    kept, removed = [head], []
    for block in blocks:
        name = tomllib.loads(block)["work"][0].get("file")
        (removed if name in names else kept).append(name if name in names else block)
    if removed:
        tmp = catalog.with_name(".catalog.toml.tmp")
        tmp.write_text("".join(kept))
        os.replace(tmp, catalog)
    return removed


def append_catalog(catalog, entry):
    """Append atomically so Syncthing never ships a half-written catalog."""
    text = catalog.read_text() if catalog.exists() else ""
    tmp = catalog.with_name(".catalog.toml.tmp")
    tmp.write_text(text.rstrip("\n") + "\n" + entry)
    os.replace(tmp, catalog)


# --- chapter folders ---------------------------------------------------------

FRONT = ("series", "front", "copyright", "dedication", "contents", "contributors",
         "foreword", "preface", "notation", "acknowledg")
BACK = ("appendix", "bibliography", "references", "index")


def chapter_key(pdf):
    """Reading order: front matter, numbered chapters, then back matter."""
    name = pdf.stem.lower()
    words = re.sub(r"[^a-z0-9]+", " ", name).split()
    lead = next((w for w in words if not w.isdigit() and w not in ("chapter", "ch", "part")), "")
    num = re.search(r"\d+", name)
    for i, w in enumerate(FRONT):
        if lead.startswith(w) or (not num and w in name):
            return (0, i, name)
    for i, w in enumerate(BACK):
        if lead.startswith(w):
            return (2, i, name)
    return (1, int(num.group()) if num else 10**6, name)


def chapter_title(pdf):
    """Bookmark text: the file name minus numbering noise and download suffixes."""
    m = re.match(r"^(\d+)a? - (.+)$", pdf.stem)  # our own 'NN - Title' naming
    if m:
        n = int(m.group(1))
        return m.group(2) if n in (0, 99) else f"{n}. {m.group(2)}"
    t = re.sub(r"_\d{4}_.*$", "", pdf.stem)  # e.g. '_2022_Handbook-of-Statis'
    t = re.sub(r"^\d+(\.\d+)?(_pp_[^_]+_[^_]+)?[_ -]*", "", t)  # '04.3_pp_37_46_'
    t = re.sub(r"^Chapter[-_ ]*(\d+)[-_ ]+", r"\1 ", t, flags=re.IGNORECASE)
    t = t.replace("---", " - ").replace("_", " ").replace("-", " ")
    t = " ".join(t.split())
    num = re.match(r"(\d+)\s+(.*)", t)
    return f"{int(num.group(1))}. {num.group(2)}" if num else t


def pdf_title(pdf):
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True,
                         check=False).stdout  # no title is fine
    m = re.search(r"^Title:\s+(.+)$", out, re.MULTILINE)
    return m.group(1).strip() if m else ""


def bookmark_titles(chapters):
    """Chapter titles for bookmarks. Download tools truncate file names, so use
    each PDF's own Title when every chapter has a distinct one (publishers that
    stamp the book title on every chapter fail that test)."""
    from_names = [chapter_title(ch) for ch in chapters]
    meta = [pdf_title(ch) for ch in chapters]
    if all(meta) and len(set(meta)) == len(meta):
        # keep the chapter number the file name gave us
        nums = [re.match(r"(\d+)\. ", t) for t in from_names]
        return [f"{n.group(1)}. {m}" if n and not re.match(r"\d", m) else m
                for n, m in zip(nums, meta)]
    return from_names


def merge_chapters(folder, log):
    """Merge a folder of chapter PDFs into '<folder>.pdf' beside it; return it."""
    import warnings

    import pikepdf

    chapters = sorted(folder.glob("*.pdf"), key=chapter_key)
    if not chapters:
        raise ValueError(f"no PDFs in {folder.name}")
    log(f"merging {len(chapters)} chapters of {folder.name}")
    out = folder.with_suffix(".pdf")
    merged = pikepdf.new()
    warnings.filterwarnings("ignore", category=pikepdf.PdfWarning if hasattr(pikepdf, "PdfWarning") else UserWarning)
    warnings.filterwarnings("ignore", message=".*named destinations.*")
    try:
        with merged.open_outline() as outline:
            for ch, title in zip(chapters, bookmark_titles(chapters), strict=True):
                with pikepdf.open(ch) as src:
                    start = len(merged.pages)
                    merged.pages.extend(src.pages)
                outline.root.append(pikepdf.OutlineItem(title, start))
        merged.save(out)
    except pikepdf.PdfError as err:
        raise ValueError(f"cannot merge {folder.name}: {err}") from err
    for ch in chapters:
        ch.unlink()
    leftovers = list(folder.iterdir())
    if leftovers:
        log(f"  left non-PDF files in {folder.name}: {', '.join(f.name for f in leftovers)}")
    else:
        folder.rmdir()
    return out


# --- driver ----------------------------------------------------------------

def settle(pdf, seconds=10):
    """Wait until a file has stopped changing (e.g. a copy still in progress)."""
    while True:
        age = time.time() - pdf.stat().st_mtime
        size = pdf.stat().st_size
        if age >= seconds:
            time.sleep(1)
            if pdf.stat().st_size == size:
                return
        time.sleep(max(1, seconds - age))


def ingest(pdf, shelf, catalog, vocab, log, is_book=False):
    """Process one PDF; returns its new file name in pdfs/ (the shelf)."""
    log(f"ingesting {pdf.relative_to(shelf.parent)}")
    settle(pdf)
    ocr_if_needed(pdf, log)
    front, pages = front_matter(pdf), page_count(pdf)
    text = claude_text(front)

    known = lookup(find_ids("\n".join(front), pdf.name), pages, log, is_book)
    if is_book and known is None:
        text = f"(This PDF was merged from a folder of chapter files.)\n{text}"
    meta = ask_claude(text, pdf.name, pages, vocab, known, log) or {}
    work = dict(meta)
    if known:  # identifier metadata wins for the bibliographic fields
        work.update({k: v for k, v in known.items() if v})
    if is_book:
        work["type"] = "book"
    work.setdefault("title", pdf.stem)
    work["year"] = pick_year(work, imprint_year("\n".join(front)))
    work["tags"] = [t.strip().lower().replace(" ", "-") for t in meta.get("tags", []) if t.strip()]
    work["review"] = known is None
    if not work.get("authors"):
        work["authors"] = []

    name = file_name(work, shelf)
    shutil.move(pdf, shelf / name)
    append_catalog(catalog, catalog_entry(name, work))
    log(f"  -> {name}  [{', '.join(work['tags'])}]" + ("  (review)" if work["review"] else ""))
    return name


# Library subdirectories that are never chapter folders.
RESERVED = {"inbox", "notes", "pdfs"}


def pending(lib, shelf, catalogued, log):
    """PDFs to ingest: anything in inbox/ or loose in the library directory,
    plus uncatalogued PDFs on the shelf. Chapter folders are merged first; a
    loose PDF that is already catalogued (old flat layout) just moves to the
    shelf."""
    box = lib / "inbox"
    merged = set()
    folders = [d for d in box.glob("*") if d.is_dir() and d.name != "failed"] if box.is_dir() else []
    folders += [d for d in lib.glob("*") if d.is_dir() and d.name not in RESERVED
                and not d.name.startswith(".")]
    for folder in sorted(folders):
        if not any(folder.glob("*.pdf")):
            continue
        for pdf in folder.glob("*.pdf"):
            settle(pdf)
        try:
            merged.add(merge_chapters(folder, log))
        except (OSError, ValueError) as err:  # unreadable/corrupt chapter PDFs
            log(f"  could not merge {folder.name}: {err}")
    has_catalog = (lib / "catalog.toml").exists()
    shelf.mkdir(exist_ok=True)
    loose = []
    for pdf in sorted(lib.glob("*.pdf")):
        if pdf.name in catalogued and not (shelf / pdf.name).exists():
            log(f"shelving catalogued {pdf.name}")
            shutil.move(pdf, shelf / pdf.name)
        elif has_catalog or pdf in merged:
            loose.append(pdf)
    inbox = sorted(box.glob("*.pdf")) if box.is_dir() else []
    # Without a catalog every shelved PDF would look new (e.g. mid first sync).
    unshelved = sorted(p for p in shelf.glob("*.pdf") if p.name not in catalogued) \
        if has_catalog else []
    return [(p, p in merged) for p in inbox + loose + unshelved]


def process(log=lambda s: print(s, flush=True)):
    lib, shelf, cat = config.LIBRARY, config.pdfs_path(), config.catalog_path()
    done = []
    for pdf, is_book in pending(lib, shelf, set(catalog.load()), log):
        vocab = {t for w in catalog.load().values() for t in w.get("tags", [])}
        try:
            done.append(ingest(pdf, shelf, cat, vocab, log, is_book))
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as err:
            # One bad PDF must not stop the rest: park it in inbox/failed/.
            log(f"  failed on {pdf.name}: {err}")
            failed = lib / "inbox" / "failed"
            failed.mkdir(parents=True, exist_ok=True)
            if pdf.exists():
                shutil.move(pdf, failed / pdf.name)
    for other in (lib / "inbox").glob("*") if (lib / "inbox").is_dir() else []:
        if other.is_file() and other.suffix.lower() != ".pdf":
            log(f"skipping non-PDF in inbox: {other.name}")
    return done

