import pytest

from books import config, index

CATALOG = """
[[work]]
file = "Barfoot - State Estimation (2025).pdf"
type = "book"
authors = ["Timothy D. Barfoot"]
title = "State Estimation for Robotics"
year = 2025
tags = ["robotics", "state-estimation"]

[[work]]
file = "Gneiting & Raftery - Scoring Rules (2007).pdf"
type = "article"
authors = ["Tilmann Gneiting", "Adrian E. Raftery"]
title = "Strictly Proper Scoring Rules"
year = 2007
tags = ["statistics", "scoring-rules"]
"""

PAGES = {
    "Barfoot - State Estimation (2025).pdf": ["Preface", "The Kalman filter updates the covariance."],
    "Gneiting & Raftery - Scoring Rules (2007).pdf": ["A proper scoring rule rewards honest forecasts."],
}


@pytest.fixture
def library(tmp_path, monkeypatch):
    lib = tmp_path / "books"
    lib.mkdir()
    (lib / "catalog.toml").write_text(CATALOG)
    for name in PAGES:
        (lib / name).write_bytes(b"%PDF-1.4 stub")
    monkeypatch.setattr(config, "LIBRARY", lib)
    monkeypatch.setattr(config, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(index, "extract", lambda pdf: PAGES[pdf.name])
    index.build()
    return lib


@pytest.mark.usefixtures("library")
def test_page_level_hits():
    hits = index.hits(["kalman", "covariance"])
    assert [(f, p) for f, p, _, _ in hits] == [("Barfoot - State Estimation (2025).pdf", 2)]
    assert hits[0][2] == "Barfoot (2025) State Estimation for Robotics"


@pytest.mark.usefixtures("library")
def test_stemming_and_metadata_match():
    assert index.hits(["forecast"])  # porter stemmer: forecasts -> forecast
    assert [f for f, *_ in index.hits(["raftery"])] == ["Gneiting & Raftery - Scoring Rules (2007).pdf"]


@pytest.mark.usefixtures("library")
def test_filters():
    assert index.hits(["rule"], tags=["robotics"]) == []
    assert [t for _, t, _, _ in index.works(kind="article")] == ["article"]
    assert dict(index.tag_counts())["statistics"] == 1


@pytest.mark.usefixtures("library")
def test_special_characters_are_literal():
    assert index.hits(["c++", "h-infinity:"]) == []  # no FTS5 syntax error


@pytest.mark.usefixtures("library")
def test_unchanged_library_is_a_no_op(capsys):
    before = config.index_path().stat().st_mtime_ns
    index.build()
    assert "index up to date" in capsys.readouterr().out
    assert config.index_path().stat().st_mtime_ns == before


def test_index_from_older_schema_is_rebuilt(tmp_path, monkeypatch):
    import sqlite3

    lib = tmp_path / "books"
    lib.mkdir()
    (lib / "catalog.toml").write_text(CATALOG)
    for name in PAGES:
        (lib / name).write_bytes(b"%PDF-1.4 stub")
    monkeypatch.setattr(config, "LIBRARY", lib)
    monkeypatch.setattr(config, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(index, "extract", lambda pdf: PAGES[pdf.name])
    # v1 layout: docs had an extra column and meta had no schema key.
    old = sqlite3.connect(config.index_path())
    old.executescript("""
        CREATE TABLE docs (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, pages INTEGER, work TEXT);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    old.close()
    index.build()
    assert [f for f, *_ in index.hits(["kalman"])] == ["Barfoot - State Estimation (2025).pdf"]
