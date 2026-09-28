import tomllib
from pathlib import Path

import pytest

from books import ingest


@pytest.mark.parametrize(("name", "expected"), [
    ("Sheldon Axler", "Axler"),
    ("A. W. van der Vaart", "van der Vaart"),
    ("Vaart, A. W. van der", "Vaart"),
    ("Luc Devroye", "Devroye"),
])
def test_surname(name, expected):
    assert ingest.surname(name) == expected


@pytest.mark.parametrize(("authors", "editors", "expected"), [
    (["Rick Durrett"], False, "Durrett"),
    (["Jorge Nocedal", "Stephen J. Wright"], False, "Nocedal & Wright"),
    (["Yi Ma", "Jana Košecká", "Stefano Soatto"], False, "Ma et al."),
    (["Frank Nielsen", "C. R. Rao"], True, "Nielsen & Rao (eds)"),
    ([], False, "Unknown"),
])
def test_short_authors(authors, editors, expected):
    assert ingest.short_authors({"authors": authors, "editors": editors}) == expected


def test_file_name_format_and_collisions(tmp_path):
    work = {"authors": ["Richard McElreath"], "title": "Statistical Rethinking: A Bayesian Course",
            "year": 2020, "edition": "2nd"}
    name = ingest.file_name(work, tmp_path)
    assert name == "McElreath - Statistical Rethinking, A Bayesian Course (2020, 2nd ed).pdf"
    (tmp_path / name).touch()
    assert ingest.file_name(work, tmp_path).endswith("(2020, 2nd ed) [2].pdf")


def test_file_name_draft_and_unsafe_chars(tmp_path):
    work = {"authors": ["Timothy D. Barfoot"], "title": 'State/Estimation? "Robotics"',
            "year": 2025, "edition": "2nd (draft)"}
    assert ingest.file_name(work, tmp_path) == "Barfoot - State-Estimation Robotics (2025, 2nd (draft)).pdf"


@pytest.mark.parametrize(("isbn", "valid"), [
    ("9780387303031", True),   # Nocedal & Wright
    ("0387303030", True),
    ("9780387303032", False),
    ("123", False),
])
def test_valid_isbn(isbn, valid):
    assert ingest.valid_isbn(isbn) is valid


def test_find_ids():
    text = """Proc. ACM ... https://doi.org/10.1145/3776729.
    arXiv:2406.15742v1 [cs.PL]
    ISBN-10: 0-387-30303-0  ISBN-13: 978-0387-30303-1"""
    ids = ingest.find_ids(text, "paper.pdf")
    assert ids == {"arxiv": "2406.15742", "doi": "10.1145/3776729",
                   "isbn": ["0387303030", "9780387303031"]}
    assert ingest.find_ids("", "2406.15742v1.pdf") == {"arxiv": "2406.15742"}


def test_chapter_order():
    names = ["Index_2022.pdf", "Chapter-10---Gaussian.pdf", "Preface_2022.pdf",
             "Chapter-2---Geometric.pdf", "Contents.pdf", "Series-Page_2022.pdf",
             "Chapter-1---Geometry.pdf", "Bibliography.pdf"]
    ordered = [p.name for p in sorted(map(Path, names), key=ingest.chapter_key)]
    assert ordered == ["Series-Page_2022.pdf", "Contents.pdf", "Preface_2022.pdf",
                       "Chapter-1---Geometry.pdf", "Chapter-2---Geometric.pdf",
                       "Chapter-10---Gaussian.pdf", "Bibliography.pdf", "Index_2022.pdf"]


@pytest.mark.parametrize(("stem", "title"), [
    ("Chapter-11---Multilevel-contours_2022_Handbook-of-Statis", "11. Multilevel contours"),
    ("04.3_pp_37_46_Interpolation_and_approximation", "Interpolation and approximation"),
    ("07 - Universal Methods", "7. Universal Methods"),
    ("00 - Front Matter", "Front Matter"),
    ("99 - Index", "Index"),
])
def test_chapter_title(stem, title):
    assert ingest.chapter_title(Path(stem + ".pdf")) == title


def test_catalog_entry_round_trips_through_toml():
    work = {"type": "book", "authors": ['Ann "Q" O\'Neil', "Bo Ek"], "title": "A: B",
            "year": 2001, "edition": "2nd", "tags": ["stats", "mcmc"], "review": True}
    entry = ingest.catalog_entry("Neil & Ek - A, B (2001, 2nd ed).pdf", work)
    parsed = tomllib.loads(entry)["work"][0]
    assert parsed["authors"] == work["authors"]
    assert parsed["title"] == "A: B"
    assert parsed["review"] is True


def test_pending_classifies_library_contents(tmp_path):
    lib, shelf = tmp_path, tmp_path / "pdfs"
    (lib / "inbox").mkdir()
    shelf.mkdir()
    (lib / "catalog.toml").write_text("")
    (lib / "inbox" / "new.pdf").touch()
    (lib / "Known - Old Layout (2000).pdf").touch()   # catalogued, old flat layout
    (lib / "dropped.pdf").touch()                     # new, dropped in the root
    (shelf / "Known - Shelved (2001).pdf").touch()    # catalogued, in place
    (shelf / "unnamed.pdf").touch()                   # on the shelf, not catalogued
    catalogued = {"Known - Old Layout (2000).pdf", "Known - Shelved (2001).pdf"}

    todo = ingest.pending(lib, shelf, catalogued, log=lambda s: None)

    assert sorted(p.relative_to(lib).as_posix() for p, _ in todo) == [
        "dropped.pdf", "inbox/new.pdf", "pdfs/unnamed.pdf"]
    assert (shelf / "Known - Old Layout (2000).pdf").exists()  # shelved, not re-ingested
    assert not any(is_book for _, is_book in todo)


CAMBRIDGE = """Downloaded from https://www.cambridge.org/core. IP address: 203.0.113.7, on 28 Sep 2026 at 11:28:52, subject to the Cambridge Core terms of use, available at
https://www.cambridge.org/core/terms. https://www.cambridge.org/core/product/819623B1B5B33836476618AC0621F0EE
FOUNDATIONS OF PROBABILISTIC PROGRAMMING
"""


def test_strip_watermarks_removes_download_stamps():
    cleaned = ingest.strip_watermarks(CAMBRIDGE)
    assert "IP address" not in cleaned and "cambridge.org/core" not in cleaned
    assert cleaned.strip() == "FOUNDATIONS OF PROBABILISTIC PROGRAMMING"


@pytest.mark.parametrize(("text", "year"), [
    ("First published 2021\nISBN 978-1-108-48851-8 Hardback", 2021),
    ("© Gilles Barthe, Joost-Pieter Katoen and Alexandra Silva 2021", 2021),
    ("Copyright © 1998, 2003 by Someone", 2003),
    ("No imprint here, but 1999 appears in prose.", None),
])
def test_imprint_year(text, year):
    assert ingest.imprint_year(text) == year


def test_claude_text_includes_late_imprint_page():
    pages = ["half title", "bio", "series", "title page", "blank", "© 2021 ... ISBN 978...", "contents"]
    text = ingest.claude_text(pages)
    assert "half title" in text and "[imprint page]" in text and "ISBN" in text
    assert "contents" not in text


def test_remove_catalog_entries(tmp_path):
    cat = tmp_path / "catalog.toml"
    cat.write_text('# header\n\n[[work]]\nfile = "A - X (2000).pdf"\ntitle = "X"\n\n'
                   '[[work]]\nfile = "B & C - \\"Y\\" (2001).pdf"\ntitle = "Y"\n')
    assert ingest.remove_catalog_entries(cat, {'B & C - "Y" (2001).pdf'}) == ['B & C - "Y" (2001).pdf']
    parsed = tomllib.loads(cat.read_text())
    assert [w["file"] for w in parsed["work"]] == ["A - X (2000).pdf"]
    assert cat.read_text().startswith("# header")
    assert ingest.remove_catalog_entries(cat, {"missing.pdf"}) == []


@pytest.mark.parametrize(("work", "imprint", "year"), [
    ({"type": "book", "year": 2020}, 2021, 2021),      # imprint beats catalogue year
    ({"type": "article", "year": 2007}, 2008, 2007),   # article: lookup year stands
    ({"type": "article"}, 2008, 2008),                 # nothing else: imprint
    ({"type": "book", "year": 2006}, None, 2006),
])
def test_pick_year(work, imprint, year):
    assert ingest.pick_year(work, imprint) == year
