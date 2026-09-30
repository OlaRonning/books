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
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from . import catalog, config, dupes
from .dupes import normalize, surname

UA = {"User-Agent": "books-ingest/1.0 (personal PDF library)"}
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

MAX_CANDIDATES = 5  # per kind; later DOIs on a short paper are its references


def find_ids(text, filename):
    """Candidate identifiers in page order, {kind: [value, ...]}. Candidates
    only: every lookup result is checked against the document (verified())."""
    arxiv = re.findall(r"arXiv:\s*(\d{4}\.\d{4,5})", text)
    m = re.match(r"(\d{4}\.\d{4,5})(v\d+)?\.pdf$", filename)
    if m:
        arxiv.insert(0, m.group(1))
    doi = [d.rstrip(".,;)") for d in re.findall(r"\b(10\.\d{4,9}/[^\s\"<>]+)", text)]
    isbn = [d for d in (re.sub(r"[^\dXx]", "", c).upper()
                        for c in re.findall(r"ISBN(?:-1[03])?:?\s*([\d\- ]{10,17}[\dXx])", text))
            if valid_isbn(d)]
    ids = {}
    for key, values in (("arxiv", arxiv), ("doi", doi), ("isbn", isbn)):
        unique = list(dict.fromkeys(values))[:MAX_CANDIDATES]
        if unique:
            ids[key] = unique
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
        "arxiv": aid,
    } if title else None


def lookup_crossref(doi):
    return crossref_work(json.loads(fetch("https://api.crossref.org/works/" + urllib.parse.quote(doi)))["message"])


def crossref_work(msg):
    """A catalog work from a Crossref record (a chapter becomes its book)."""
    people = msg.get("author") or msg.get("editor") or []
    # Print year is the citation year; "issued" is the earliest (often online-first).
    dated = msg.get("published-print") or msg.get("issued") or msg.get("published") or {}
    parts = dated.get("date-parts", [[None]])[0]
    kind = msg.get("type", "")
    container = (msg.get("container-title") or [""])[0]
    if kind not in ARTICLE_TYPES and kind not in BOOK_TYPES and container:
        # A chapter (Cambridge tags these "other"): catalogue the book instead.
        for isbn in msg.get("ISBN", []):
            book = lookup_isbn(re.sub(r"[^\dXx]", "", isbn))
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
        "doi": msg.get("DOI"),
    }
    if not msg.get("author") and msg.get("editor"):
        work["editors"] = True
    venue = (msg.get("container-title") or [""])[0]
    if venue and work["type"] == "article":
        vol = msg.get("volume")
        work["venue"] = f"{venue} {vol}" if vol else venue
    return work if work["title"] else None


def lookup_isbn(isbn):
    docs = json.loads(fetch(
        f"https://openlibrary.org/search.json?isbn={isbn}"
        "&fields=title,subtitle,author_name,first_publish_year"))["docs"]
    if not (docs and docs[0].get("title") and docs[0].get("author_name")):
        return None
    d = docs[0]
    return {
        "type": "book",
        "title": ": ".join(filter(None, [d["title"], d.get("subtitle")])),
        "authors": d["author_name"],
        "year": d.get("first_publish_year"),
        "isbn": dupes.isbn13(isbn.lower()),
    }


def search_crossref(title, author):
    """Crossref bibliographic search, for works whose PDF carries no identifier."""
    q = urllib.parse.urlencode({"query.bibliographic": title, "query.author": author, "rows": 5})
    items = json.loads(fetch(f"https://api.crossref.org/works?{q}"))["message"]["items"]
    return best_crossref_match(items, title, author)


def best_crossref_match(items, title, author):
    """The first search hit with a near-identical title and the same first author."""
    for msg in items:
        main = (msg.get("title") or [""])[0]
        full = ": ".join(filter(None, [main, (msg.get("subtitle") or [""])[0]]))
        names = {normalize(p.get("family", "")) for p in (msg.get("author") or msg.get("editor") or [])}
        close = max(dupes.title_similarity(main, title), dupes.title_similarity(full, title))
        if close >= SEARCH_SIMILARITY and normalize(author) in names:
            return crossref_work(msg)
    return None


SEARCH_SIMILARITY = 0.92


def search_verified(work, front, is_book, log):
    """Crossref search by title and first author, accepted only if the hit
    also describes this PDF (a wrong catalog title must not confirm itself)."""
    try:
        hit = search_crossref(work["title"], surname(work["authors"][0]))
    except (OSError, ValueError, KeyError) as err:
        log(f"  crossref search failed: {err}")
        return None
    if not hit:
        return None
    if not verified(hit, front, is_book):
        log(f"  crossref search found \"{hit['title'][:60]}\", not this PDF; skipping")
        return None
    expected = "book" if is_book else work.get("type")
    if expected and hit["type"] != expected:
        # e.g. Thrun's 2002 CACM article "Probabilistic Robotics" is not the book.
        log(f"  crossref search found a {hit['type']} for this {expected}; skipping")
        return None
    hit.pop("crossref_type", None)
    log(f"  identified via crossref search: doi {hit.get('doi')}")
    return hit


def verified(work, front, is_book):
    """Does a lookup result describe this PDF? Its main title (and, if known,
    first author) must appear on the opening pages: page 1-2 for a paper,
    so a DOI from its reference list cannot pass, and page 1-5 for a book."""
    opening = normalize(" ".join(front[:5 if is_book else 2]))

    def present(word):
        # PDFs glue affiliation/footnote markers to words: "Chen1", "Optimization2".
        return re.search(rf"(?<![a-z0-9]){re.escape(word)}\d*(?![a-z0-9])", opening) is not None

    words = [w for w in normalize(work.get("title", "").split(":")[0]).split() if len(w) > 2]
    if not words or sum(map(present, words)) / len(words) < 0.8:
        return False
    author = dupes.first_author(work)
    return not author or all(map(present, author.split()))


def lookup(ids, pages, front, log, is_book=False):
    """The first identifier (in an order that suits the document's length)
    whose record verifiably describes this PDF."""
    is_book = is_book or pages > BOOK_PAGES
    order = ["isbn", "doi", "arxiv"] if is_book else ["arxiv", "doi", "isbn"]
    fns = {"arxiv": lookup_arxiv, "doi": lookup_crossref, "isbn": lookup_isbn}
    for key in order:
        for value in ids.get(key, []):
            try:
                work = fns[key](value)
            except (OSError, ValueError, KeyError, ET.ParseError) as err:
                # network (URLError is an OSError), JSON/XML parse, missing fields
                log(f"  {key} {value} lookup failed: {err}")
                continue
            if not (work and work.get("title") and work.get("authors")):
                continue
            # A long PDF whose DOI is a chapter/article DOI is not that article.
            if key == "doi" and is_book and work["type"] != "book":
                log(f"  ignoring DOI {value} ({work.get('crossref_type')}) for a {pages}-page PDF")
                continue
            if not verified(work, front, is_book):
                log(f"  {key} {value} is \"{work['title'][:60]}\", not this PDF; skipping")
                continue
            work.pop("crossref_type", None)
            log(f"  identified via {key} {value}")
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


def base_name(work):
    """'Author - Title (Year[, ed]).pdf' for a work, before collision handling."""
    title = clean(work["title"])
    if len(title) > 120:
        title = title[:120].rsplit(" ", 1)[0]
    bits = [str(work["year"])] if work.get("year") else []
    ed = work.get("edition")
    if ed:
        bits.append(ed if "draft" in ed else f"{ed} ed")
    suffix = f" ({', '.join(bits)})" if bits else ""
    base = f"{clean(short_authors(work))} - {title}{suffix}"
    return f"{base}.pdf"


def file_name(work, shelf):
    """base_name(work), made unique within `shelf` with a ' [n]' suffix."""
    name = base_name(work)
    stem, n = name[:-4], 2
    while (shelf / name).exists():
        name, n = f"{stem} [{n}].pdf", n + 1
    return name


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


REVIEW_HINTS = ("not this PDF", "lookup failed", "search failed", "claude failed",
                "ignoring DOI", "skipping")


def review_reason(ids, notes, meta):
    """Why a work could not be confirmed, in one line for `books review`."""
    parts = [] if ids else [f"no DOI, arXiv id or ISBN in the first {FRONT_PAGES} pages"]
    parts += [n for n in notes if any(h in n for h in REVIEW_HINTS)]
    if not (meta.get("title") and meta.get("authors")):
        parts.append("Claude could not read the title and authors")
    elif not any("crossref search" in n for n in notes):
        parts.append("Crossref title search found no confident match")
    return "; ".join(dict.fromkeys(parts))[:400]


class Duplicate(Exception):
    """The PDF is already in the library: args are (existing file, reason)."""


def ingest(pdf, shelf, cat_path, works, hashes, log, is_book=False):
    """Process one PDF; returns its new file name in pdfs/ (the shelf).
    Raises Duplicate when it is already catalogued."""
    log(f"ingesting {pdf.relative_to(shelf.parent)}")
    settle(pdf)
    marker = keep_marker(pdf)
    keep = marker.exists()  # `books review`: keep this one despite a duplicate
    digest = dupes.sha256(pdf)
    if digest in hashes and not keep:
        raise Duplicate(hashes[digest], "identical file")
    ocr_if_needed(pdf, log)
    front, pages = front_matter(pdf), page_count(pdf)
    text = claude_text(front)
    vocab = {t for w in works.values() for t in w.get("tags", [])}
    notes = []  # what went wrong identifying this PDF, for `books review`

    def note(msg):
        notes.append(msg.strip())
        log(msg)

    ids = find_ids("\n".join(front), pdf.name)
    known = lookup(ids, pages, front, note, is_book)
    if is_book and known is None:
        text = f"(This PDF was merged from a folder of chapter files.)\n{text}"
    meta = ask_claude(text, pdf.name, pages, vocab, known, note) or {}
    if known is None and meta.get("title") and meta.get("authors"):
        known = search_verified(meta, front, is_book or pages > BOOK_PAGES, note)
    work = dict(meta)
    if known:  # identifier metadata wins for the bibliographic fields
        work.update({k: v for k, v in known.items() if v})
    if is_book:
        work["type"] = "book"
    work.setdefault("title", pdf.stem)
    work["year"] = pick_year(work, imprint_year("\n".join(front)))
    work["tags"] = [t.strip().lower().replace(" ", "-") for t in meta.get("tags", []) if t.strip()]
    work["review"] = review_reason(ids, notes, meta) if known is None else False
    if not work.get("authors"):
        work["authors"] = []
    match = None if keep else dupes.find_match(work, works)
    if match:
        raise Duplicate(*match)

    name = file_name(work, shelf)
    shutil.move(pdf, shelf / name)
    catalog.append(catalog.entry_text(name, work), cat_path)
    marker.unlink(missing_ok=True)
    hashes[digest] = name
    log(f"  -> {name}  [{', '.join(work['tags'])}]" + ("  (review)" if work["review"] else ""))
    return name


# Library and inbox subdirectories that are never chapter folders.
RESERVED = {"annotated", "annotations", "inbox", "notes", "pdfs"}
PARKED = {"failed", "duplicates"}


def pending(lib, shelf, catalogued, log):
    """PDFs to ingest: anything in inbox/ or loose in the library directory,
    plus uncatalogued PDFs on the shelf. Chapter folders are merged first; a
    loose PDF that is already catalogued (old flat layout) just moves to the
    shelf."""
    box = lib / "inbox"
    merged = set()
    folders = [d for d in box.glob("*") if d.is_dir() and d.name not in PARKED] if box.is_dir() else []
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


def keep_marker(pdf):
    """`<name>.keep-duplicate` beside an inbox PDF: skip the duplicate check."""
    return pdf.with_name(pdf.name + KEEP_SUFFIX)


KEEP_SUFFIX = ".keep-duplicate"
WHY_SUFFIX = ".why"


def park(pdf, lib, where, why):
    """Move a PDF to inbox/<where>/ with a `<name>.why` note for `books review`."""
    dest = lib / "inbox" / where
    dest.mkdir(parents=True, exist_ok=True)
    if pdf.exists():
        shutil.move(pdf, dest / pdf.name)
        (dest / (pdf.name + WHY_SUFFIX)).write_text(why + "\n")


MAX_PASSES = 20


def process(log=lambda s: print(s, flush=True)):
    """Ingest everything pending, re-scanning until a pass finds nothing:
    the path unit ignores events while this runs, so files that arrive
    mid-run (e.g. a sync delivering a batch in waves) join this run instead
    of waiting for the nightly sweep. Every item leaves the queue (shelved or
    parked), so passes terminate."""
    lib, shelf, cat = config.LIBRARY, config.pdfs_path(), config.catalog_path()
    hashes, done = None, []
    for _ in range(MAX_PASSES):
        todo = pending(lib, shelf, set(catalog.load()), log)
        if not todo:
            break
        if hashes is None:
            hashes = {dupes.sha256(p): p.name for p in shelf.glob("*.pdf")}
        for pdf, is_book in todo:
            try:
                done.append(ingest(pdf, shelf, cat, catalog.load(), hashes, log, is_book))
            except Duplicate as dup:
                existing, reason = dup.args
                log(f"  duplicate of {existing} ({reason}); parked in inbox/duplicates/")
                park(pdf, lib, "duplicates", f"{existing}\t{reason}")
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as err:
                # One bad PDF must not stop the rest: park it in inbox/failed/.
                log(f"  failed on {pdf.name}: {err}")
                park(pdf, lib, "failed", f"{type(err).__name__}: {err}")
    for other in (lib / "inbox").glob("*") if (lib / "inbox").is_dir() else []:
        if other.is_file() and other.suffix.lower() != ".pdf" and not other.name.endswith(KEEP_SUFFIX):
            log(f"skipping non-PDF in inbox: {other.name}")
    return done



def backfill_ids(works, log=lambda s: print(s, flush=True)):
    """Add verified DOI/arXiv/ISBN fields to catalog entries that lack them,
    so identifier-based duplicate detection covers the whole library."""
    shelf, updates = config.pdfs_path(), {}
    for file, work in sorted(works.items()):
        pdf = shelf / file
        if any(work.get(k) for k in dupes.ID_KEYS) or not pdf.exists():
            continue
        front, pages = front_matter(pdf), page_count(pdf)
        is_book = work.get("type") == "book"
        is_book = is_book or pages > BOOK_PAGES
        found = lookup(find_ids("\n".join(front), file), pages, front, lambda _: None, is_book)
        if not found and work.get("authors"):
            found = search_verified(work, front, is_book, lambda msg, f=file: log(f"  {f}:{msg}"))
        main = work["title"].split(":")[0]
        if found and dupes.title_similarity(found.get("title", "").split(":")[0], main) >= 0.85:
            ids = {k: found[k] for k in dupes.ID_KEYS if found.get(k)}
            if ids:
                updates[file] = ids
                log(f"  {file}: {', '.join(f'{k} {v}' for k, v in ids.items())}")
    return catalog.update_entries(updates) if updates else []
