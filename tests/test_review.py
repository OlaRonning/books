import tomllib

import pytest

from books import catalog, config, ingest, review

CATALOG = """# head

[[work]]
file = "Briales - Cartan-Sync (2017).pdf"
type = "article"
authors = ["Jesus Briales"]
title = "Cartan-Sync"
year = 2017
tags = ["slam"]
review = true  # identified without a lookup; check me

[[work]]
file = "Liu & Wang - SVGD (2016).pdf"
type = "article"
authors = ["Qiang Liu", "Dilin Wang"]
title = "SVGD"
year = 2016
arxiv = "1608.04471"
tags = ["mcmc"]
"""


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIBRARY", tmp_path)
    (tmp_path / "catalog.toml").write_text(CATALOG)
    shelf = tmp_path / "pdfs"
    shelf.mkdir()
    for name in catalog.load():
        (shelf / name).write_bytes(b"%PDF stub")
    dups, failed = tmp_path / "inbox" / "duplicates", tmp_path / "inbox" / "failed"
    dups.mkdir(parents=True)
    failed.mkdir()
    (dups / "svgd.pdf").write_bytes(b"%PDF copy")
    (dups / "svgd.pdf.why").write_text("Liu & Wang - SVGD (2016).pdf\tsame arxiv\n")
    (failed / "broken.pdf").write_bytes(b"not a pdf")
    (failed / "broken.pdf.why").write_text("CalledProcessError: pdftotext failed\n")
    return tmp_path


def works():
    return catalog.load()


def test_items_lists_all_three_kinds(lib):
    kinds = {(k, n) for k, n, _ in review.items()}
    assert kinds == {("review", "Briales - Cartan-Sync (2017).pdf"), ("dup", "svgd.pdf"),
                     ("failed", "broken.pdf")}
    dup = next(d for k, _, d in review.items() if k == "dup")
    assert "Liu & Wang - SVGD (2016).pdf" in dup and "same arxiv" in dup


def test_accept_clears_only_the_flag(lib):
    assert review.accept("Briales - Cartan-Sync (2017).pdf")
    w = works()["Briales - Cartan-Sync (2017).pdf"]
    assert "review" not in w and w["title"] == "Cartan-Sync"
    assert (lib / "catalog.toml").read_text().startswith("# head")


def test_edit_renames_the_pdf_and_clears_the_flag(lib):
    old = "Briales - Cartan-Sync (2017).pdf"
    block = catalog.entry_block(old)
    assert block is not None
    text = block.replace(
        'authors = ["Jesus Briales"]', 'authors = ["Jesus Briales", "Javier Gonzalez-Jimenez"]'
    ).replace('title = "Cartan-Sync"', 'title = "Cartan-Sync: Fast and Global SE(d)-Synchronization"')
    new = review.apply_edit(old, text)
    assert new == "Briales & Gonzalez-Jimenez - Cartan-Sync, Fast and Global SE(d)-Synchronization (2017).pdf"
    assert (lib / "pdfs" / new).exists() and not (lib / "pdfs" / old).exists()
    w = works()[new]
    assert w["authors"][1] == "Javier Gonzalez-Jimenez" and "review" not in w


def test_invalid_edit_is_rejected_and_changes_nothing(lib):
    before = (lib / "catalog.toml").read_text()
    with pytest.raises(ValueError):
        review.apply_edit("Briales - Cartan-Sync (2017).pdf", '[[work]]\ntitle = "no file"\n')
    with pytest.raises(tomllib.TOMLDecodeError):
        review.apply_edit("Briales - Cartan-Sync (2017).pdf", "not = toml = at all")
    assert (lib / "catalog.toml").read_text() == before


def test_redo_requeues(lib):
    review.redo("Briales - Cartan-Sync (2017).pdf")
    assert "Briales - Cartan-Sync (2017).pdf" not in works()
    assert (lib / "inbox" / "Briales - Cartan-Sync (2017).pdf").exists()


def test_duplicate_actions(lib):
    review.keep_both("svgd.pdf")
    assert (lib / "inbox" / "svgd.pdf").exists()
    assert ingest.keep_marker(lib / "inbox" / "svgd.pdf").exists()
    assert not (lib / "inbox" / "duplicates" / "svgd.pdf.why").exists()


def test_replace_existing_drops_the_catalogued_copy(lib):
    review.replace_existing("svgd.pdf")
    assert "Liu & Wang - SVGD (2016).pdf" not in works()
    assert not (lib / "pdfs" / "Liu & Wang - SVGD (2016).pdf").exists()
    assert (lib / "inbox" / "svgd.pdf").exists()


def test_keep_existing_and_failed_actions(lib):
    review.keep_existing("svgd.pdf")
    review.retry("broken.pdf")
    assert not list((lib / "inbox" / "duplicates").iterdir())
    assert (lib / "inbox" / "broken.pdf").exists() and not (lib / "inbox" / "failed" / "broken.pdf.why").exists()
    assert review.items() == [("review", "Briales - Cartan-Sync (2017).pdf", review.items()[0][2])]


def test_keep_marker_skips_duplicate_check(lib, monkeypatch):
    """A PDF marked keep-duplicate is ingested even though its id matches."""
    review.keep_both("svgd.pdf")
    work = {"type": "article", "authors": ["Qiang Liu"], "title": "SVGD", "year": 2016,
            "arxiv": "1608.04471", "tags": []}
    monkeypatch.setattr(ingest, "settle", lambda pdf: None)
    monkeypatch.setattr(ingest, "ocr_if_needed", lambda pdf, log: None)
    monkeypatch.setattr(ingest, "front_matter", lambda pdf: ["SVGD\nQiang Liu"])
    monkeypatch.setattr(ingest, "page_count", lambda pdf: 10)
    monkeypatch.setattr(ingest, "lookup", lambda *a, **k: dict(work))
    monkeypatch.setattr(ingest, "ask_claude", lambda *a, **k: {"tags": ["mcmc"]})
    pdf = lib / "inbox" / "svgd.pdf"
    name = ingest.ingest(pdf, lib / "pdfs", lib / "catalog.toml", works(), {}, lambda _: None)
    # Catalogued as a work of its own instead of raising Duplicate (same arxiv).
    assert name in works() and (lib / "pdfs" / name).exists()
    assert "Liu & Wang - SVGD (2016).pdf" in works()
    assert not ingest.keep_marker(pdf).exists()
