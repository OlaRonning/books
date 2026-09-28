"""Duplicate detection: the same file, the same identifier, or the same work.

Rules, strongest first:
  file   identical bytes (sha256)
  id     a shared DOI, arXiv id or ISBN (normalised)
  title  same first-author surname and near-identical title

Matches are reported, never acted on destructively: ingest parks a duplicate
in inbox/duplicates/, and `books dupes` lists suspects for a human to judge
(a preprint and its published version, or two editions, may both be wanted).
"""

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher
from itertools import combinations

ID_KEYS = ("doi", "arxiv", "isbn")
TITLE_SIMILARITY = 0.9
PARTICLES = {"van", "von", "der", "den", "de", "da", "di", "du", "la", "le", "del"}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def normalize(text):
    """Lowercase ASCII words: accents, punctuation and '&' vs 'and' ignored."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def title_similarity(a, b):
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def norm_id(key, value):
    value = str(value).strip().lower()
    if key == "doi":
        return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", value)
    if key == "arxiv":
        return re.sub(r"v\d+$", "", value.removeprefix("arxiv:"))
    if key == "isbn":
        return isbn13(re.sub(r"[^0-9x]", "", value))
    return value


def isbn13(isbn):
    """ISBN-10s become ISBN-13s so both forms of one book compare equal."""
    if len(isbn) != 10:
        return isbn
    core = "978" + isbn[:9]
    check = (10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core)) % 10) % 10
    return core + str(check)


def surname(name):
    if "," in name:
        return name.split(",")[0].strip()
    toks = name.split()
    for i, t in enumerate(toks[1:], 1):
        if t.lower() in PARTICLES:
            return " ".join(toks[i:])
    return toks[-1] if toks else ""


def first_author(work):
    authors = work.get("authors") or []
    return normalize(surname(authors[0])) if authors else ""


def same_work(a, b):
    """Why two catalog entries look like one work ('id'/'title'), or None."""
    for key in ID_KEYS:
        if a.get(key) and b.get(key) and norm_id(key, a[key]) == norm_id(key, b[key]):
            return f"same {key}"
    fa = first_author(a)
    if fa and fa == first_author(b) and \
            title_similarity(a.get("title", ""), b.get("title", "")) >= TITLE_SIMILARITY:
        return "same first author and title"
    return None


def find_match(work, works):
    """The first catalogued (file, reason) that `work` duplicates, or None."""
    for file, other in works.items():
        reason = same_work(work, other)
        if reason:
            return file, reason
    return None


def report(works, hashes):
    """Suspected duplicate pairs as (file, file, reason); hashes: {file: sha}."""
    pairs = []
    by_hash = {}
    for file, h in hashes.items():
        by_hash.setdefault(h, []).append(file)
    for files in by_hash.values():
        pairs += [(a, b, "identical file") for a, b in combinations(sorted(files), 2)]
    seen = {frozenset(p[:2]) for p in pairs}
    for (fa, a), (fb, b) in combinations(sorted(works.items()), 2):
        reason = same_work(a, b)
        if reason and frozenset((fa, fb)) not in seen:
            pairs.append((fa, fb, reason))
    return pairs
