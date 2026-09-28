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
    assert ids == {"arxiv": ["2406.15742"], "doi": ["10.1145/3776729"],
                   "isbn": ["0387303030", "9780387303031"]}
    assert ingest.find_ids("", "2406.15742v1.pdf") == {"arxiv": ["2406.15742"]}


def test_find_ids_keeps_page_order_and_caps_candidates():
    refs = "\n".join(f"[{i}] doi 10.1000/ref{i}" for i in range(9))
    ids = ingest.find_ids("doi 10.1109/own.2019\n" + refs, "x.pdf")
    assert ids["doi"][0] == "10.1109/own.2019"
    assert len(ids["doi"]) == ingest.MAX_CANDIDATES


MH_ISAM2 = ["MH-iSAM2: Multi-hypothesis iSAM using Bayes Tree and Hypo-tree\nMing Hsiao and Michael Kaess",
            "I. INTRODUCTION Simultaneous localization and mapping ..."]


def test_verified_rejects_a_reference_list_doi():
    # Hsiao19icra.pdf once resolved to a DOI from its own reference list.
    davis = {"title": "A column approximate minimum degree ordering algorithm",
             "authors": ["Timothy A. Davis", "John R. Gilbert"]}
    mh = {"title": "MH-iSAM2: Multi-hypothesis iSAM using Bayes Tree and Hypo-tree",
          "authors": ["Ming Hsiao", "Michael Kaess"]}
    assert not ingest.verified(davis, MH_ISAM2, is_book=False)
    assert ingest.verified(mh, MH_ISAM2, is_book=False)


def test_best_crossref_match_needs_title_and_author():
    items = [
        {"title": ["Pose graph optimization in the complex domain"], "author": [{"family": "Someone"}],
         "type": "journal-article", "DOI": "10.1/wrong-author"},
        {"title": ["Pose Graph Optimization in the Complex Domain"],
         "subtitle": ["Duality, Optimal Solutions, and Verification"],
         "author": [{"given": "Luca", "family": "Carlone"}], "type": "journal-article",
         "DOI": "10.1109/TRO.2016.2544304", "issued": {"date-parts": [[2016]]}},
    ]
    title = "Pose Graph Optimization in the Complex Domain: Duality, Optimal Solutions, and Verification"
    hit = ingest.best_crossref_match(items, title, "Carlone")
    assert hit and hit["doi"] == "10.1109/TRO.2016.2544304" and hit["year"] == 2016
    assert ingest.best_crossref_match(items, "Something Else Entirely", "Carlone") is None


def test_update_catalog_entries_adds_missing_fields_only(tmp_path):
    cat = tmp_path / "catalog.toml"
    cat.write_text('# head\n\n[[work]]\nfile = "a.pdf"\ntitle = "A"\ndoi = "10.1/keep"\n\n'
                   '[[work]]\nfile = "b.pdf"\ntitle = "B"\n')
    done = ingest.update_catalog_entries(cat, {"a.pdf": {"doi": "10.1/other", "arxiv": "1234.5678"},
                                               "b.pdf": {"isbn": "9780387303031"}})
    works = {w["file"]: w for w in tomllib.loads(cat.read_text())["work"]}
    assert sorted(done) == ["a.pdf", "b.pdf"]
    assert works["a.pdf"]["doi"] == "10.1/keep" and works["a.pdf"]["arxiv"] == "1234.5678"
    assert works["b.pdf"]["isbn"] == "9780387303031"
    assert cat.read_text().startswith("# head")


def test_parked_folders_are_never_merged(tmp_path):
    lib, shelf = tmp_path, tmp_path / "pdfs"
    for parked in ("duplicates", "failed"):
        (lib / "inbox" / parked).mkdir(parents=True)
        (lib / "inbox" / parked / "x.pdf").touch()
    assert ingest.pending(lib, shelf, set(), log=lambda _: None) == []
    assert (lib / "inbox" / "duplicates" / "x.pdf").exists()


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

    todo = ingest.pending(lib, shelf, catalogued, log=lambda _: None)

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


def test_search_hit_must_describe_the_pdf(monkeypatch):
    # A wrong catalog title ("Davis et al.") must not confirm itself via search.
    davis = {"title": "A column approximate minimum degree ordering algorithm",
             "authors": ["Timothy A. Davis"], "doi": "10.1145/1024074.1024079"}
    monkeypatch.setattr(ingest, "search_crossref", lambda _title, _author: dict(davis))
    assert ingest.search_verified(davis, MH_ISAM2, False, log=lambda _: None) is None
    mh = {"title": "MH-iSAM2: Multi-hypothesis iSAM using Bayes Tree and Hypo-tree",
          "authors": ["Ming Hsiao", "Michael Kaess"], "doi": "10.1109/ICRA.2019.8793854"}
    monkeypatch.setattr(ingest, "search_crossref", lambda _title, _author: dict(mh))
    hit = ingest.search_verified(mh, MH_ISAM2, False, log=lambda _: None)
    assert hit is not None and hit["doi"] == mh["doi"]


def test_process_picks_up_files_that_arrive_mid_run(tmp_path, monkeypatch):
    from books import config

    lib = tmp_path
    (lib / "pdfs").mkdir()
    (lib / "catalog.toml").write_text("")
    monkeypatch.setattr(config, "LIBRARY", lib)
    arrivals = [[lib / "a.pdf"], [lib / "b.pdf"], []]  # b.pdf lands during a.pdf's run
    monkeypatch.setattr(ingest, "pending", lambda *_a, **_k: [(p, False) for p in arrivals.pop(0)])
    seen = []
    monkeypatch.setattr(ingest, "ingest", lambda pdf, *_a, **_k: seen.append(pdf.name) or pdf.name)
    assert ingest.process(log=lambda _: None) == ["a.pdf", "b.pdf"]
    assert seen == ["a.pdf", "b.pdf"]


def test_verified_tolerates_footnote_markers():
    # arXiv 2306.07225 prints "Zhaozhong Chen1 , Harel Biggie2"; it was wrongly rejected.
    first_page = ("Kalman Filter Auto-tuning through Enforcing\nChi-Squared Normalized Error Distributions\n"
                  "with Bayesian Optimization\nZhaozhong Chen1 , Harel Biggie2 , Nisar Ahmed3")
    front = [first_page, ""]
    work = {"title": "Kalman Filter Auto-tuning through Enforcing Chi-Squared Normalized Error "
                     "Distributions with Bayesian Optimization", "authors": ["Zhaozhong Chen"]}
    assert ingest.verified(work, front, is_book=False)
    assert not ingest.verified({**work, "authors": ["Ann Chenoweth"]}, front, is_book=False)


def test_search_hit_must_match_the_work_type(monkeypatch):
    front = ["PROBABILISTIC ROBOTICS\nSebastian THRUN\nWolfram BURGARD\nDieter FOX", ""]
    article = {"type": "article", "title": "Probabilistic robotics", "authors": ["Sebastian Thrun"],
               "doi": "10.1145/504729.504754"}
    monkeypatch.setattr(ingest, "search_crossref", lambda _title, _author: dict(article))
    book = {"type": "book", "title": "Probabilistic Robotics", "authors": ["Sebastian Thrun"]}
    assert ingest.search_verified(book, front, True, log=lambda _: None) is None


def test_crossref_year_prefers_print_over_online_first():
    msg = {"type": "journal-article", "title": ["Control functionals for Monte Carlo integration"],
           "author": [{"given": "Chris", "family": "Oates"}], "DOI": "10.1111/rssb.12185",
           "issued": {"date-parts": [[2016, 5]]}, "published-print": {"date-parts": [[2017, 6]]}}
    work = ingest.crossref_work(msg)
    assert work is not None and work["year"] == 2017
