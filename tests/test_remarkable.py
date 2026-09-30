import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from books import config, ingest, remarkable

DATA = Path(__file__).parent / "data"
NAME = "Liu & Wang - SVGD (2016)"
CATALOG = f"""
[[work]]
file = "{NAME}.pdf"
type = "article"
authors = ["Qiang Liu", "Dilin Wang"]
title = "SVGD"
year = 2016
tags = ["mcmc"]
"""


def make_archive(path, pages):
    """A document archive as the cloud serves it: pages is [(page id, .rm
    file or None, PDF page index or None for a page added on the tablet)]."""
    doc = "5a3ce208-0000-0000-0000-000000000000"
    content = {"fileType": "pdf", "cPages": {"pages": [
        {"id": pid, "idx": {"timestamp": "1:1", "value": f"b{i:02}"},
         **({"redir": {"timestamp": "1:1", "value": redir}} if redir is not None else {})}
        for i, (pid, _, redir) in enumerate(pages)]}}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{doc}.content", json.dumps(content))
        z.writestr(f"{doc}.metadata", json.dumps({"visibleName": NAME}))
        z.writestr(f"{doc}.pdf", b"%PDF stub")
        for pid, rm, _ in pages:
            if rm:
                z.write(DATA / rm, f"{doc}/{pid}.rm")
    return path


class Cloud:
    """Stands in for rmapi: a folder of named documents, each an archive."""

    def __init__(self, tmp_path, docs=()):
        self.folder = {n: make_archive(tmp_path / f"cloud-{n}.zip", [("p1", "highlighted.rm", 4)])
                       for n in docs}
        self.calls = []

    def __call__(self, *args, cwd=None, check=True):
        self.calls.append(args)
        out = ""
        if args[:2] == ("-json", "ls"):
            items = ([{"name": n, "type": "DocumentType"} for n in self.folder]
                     if args[2] == "/Books" else [{"name": "Books", "type": "CollectionType"}])
            out = json.dumps(items)
        elif args[0] == "put":
            self.folder[Path(args[1]).stem] = Path(args[1])
        elif args[0] == "geta":
            name = args[2].removeprefix("/Books/")
            Path(cwd or ".", f"{name}.zip").write_bytes(self.folder[name].read_bytes())
            Path(cwd or ".", f"{name}-annotations.pdf").write_bytes(b"%PDF annotated")
        elif args[0] == "rm":
            self.folder.pop(args[1].removeprefix("/Books/").replace("\\", ""))
        return subprocess.CompletedProcess(args, 0, out, "")


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "lib")
    monkeypatch.setattr(config, "TABLET_FOLDER", "/Books")
    monkeypatch.setattr(remarkable, "page_count", lambda pdf: None)
    (tmp_path / "lib" / "pdfs").mkdir(parents=True)
    (tmp_path / "lib" / "catalog.toml").write_text(CATALOG)
    (tmp_path / "lib" / "pdfs" / f"{NAME}.pdf").write_bytes(b"%PDF original")
    return tmp_path / "lib"


def test_page_map_orders_pages_and_marks_added_ones():
    content = {"cPages": {"pages": [
        {"id": "b", "idx": {"value": "b2"}, "redir": {"value": 1}},
        {"id": "gone", "idx": {"value": "b3"}, "redir": {"value": 2}, "deleted": {"value": 1}},
        {"id": "a", "idx": {"value": "b1"}, "redir": {"value": 0}},
        {"id": "new", "idx": {"value": "b1x"}},
    ]}}
    assert remarkable.page_map(content) == [("a", 1), ("new", None), ("b", 2)]
    legacy = {"pages": ["x", "y", "z"], "redirectionPageMap": [0, -1, 1]}
    assert remarkable.page_map(legacy) == [("x", 1), ("y", None), ("z", 2)]


def test_highlights_join_lines_and_keep_pdf_pages(tmp_path):
    archive = make_archive(tmp_path / "a.zip", [("p1", "highlighted.rm", 4), ("p2", None, None)])
    [(page, found)] = remarkable.highlights(archive)
    assert page == 5
    assert found[0] == ("The reMarkable uses electronic paper", "yellow")
    # One highlight over three lines comes back as one passage.
    assert len(found) == 2
    assert found[1][0].startswith("ReMarkable uses its own operating system, named Codex. Codex is based")
    assert found[1][0].endswith("display technology.[13]")


def test_highlight_colors_by_rgba(tmp_path):
    archive = make_archive(tmp_path / "a.zip", [("p1", "colors.rm", 0)])
    [(_, found)] = remarkable.highlights(archive)
    assert [c for _, c in found] == ["yellow", "blue", "pink", "orange", "green", "gray"]


def test_glob_escape_makes_rm_literal():
    assert remarkable.glob_escape("A [draft] *v2?") == r"A \[draft\] \*v2\?"


def test_push_uploads_the_pdf_by_file_name(lib, tmp_path, monkeypatch):
    cloud = Cloud(tmp_path)
    monkeypatch.setattr(remarkable, "rmapi", cloud)
    remarkable.push(["svgd"])
    assert cloud.folder[NAME] == lib / "pdfs" / f"{NAME}.pdf"


def test_push_restores_an_earlier_reading(lib, tmp_path, monkeypatch):
    (lib / "annotations").mkdir()
    archive = make_archive(lib / "annotations" / f"{NAME}.rmdoc", [("p1", "highlighted.rm", 0)])
    cloud = Cloud(tmp_path)
    monkeypatch.setattr(remarkable, "rmapi", cloud)
    remarkable.push([f"{NAME}.pdf"])
    assert cloud.folder[NAME] == archive


def test_push_refuses_a_second_copy(lib, tmp_path, monkeypatch):
    monkeypatch.setattr(remarkable, "rmapi", Cloud(tmp_path, [NAME]))
    with pytest.raises(SystemExit, match="already on the tablet"):
        remarkable.push(["svgd"])


def test_pull_archives_renders_extracts_and_removes(lib, tmp_path, monkeypatch):
    cloud = Cloud(tmp_path, [NAME])
    monkeypatch.setattr(remarkable, "rmapi", cloud)
    remarkable.pull(["svgd"])
    assert remarkable.check_archive(lib / "annotations" / f"{NAME}.rmdoc") is None
    assert (lib / "annotated" / f"{NAME}.pdf").read_bytes() == b"%PDF annotated"
    notes = (lib / "annotated" / f"{NAME}.md").read_text()
    assert "## p. 5" in notes and "- The reMarkable uses electronic paper" in notes
    assert (lib / "pdfs" / f"{NAME}.pdf").read_bytes() == b"%PDF original"
    assert NAME not in cloud.folder
    assert ("rm", f"/Books/{NAME}") in cloud.calls


def test_pull_keep_leaves_it_on_the_tablet(lib, tmp_path, monkeypatch):
    cloud = Cloud(tmp_path, [NAME])
    monkeypatch.setattr(remarkable, "rmapi", cloud)
    remarkable.pull([NAME], keep=True)
    assert NAME in cloud.folder
    assert not any(c[0] == "rm" for c in cloud.calls)


def test_pull_keeps_it_on_the_tablet_when_the_download_is_bad(lib, tmp_path, monkeypatch):
    cloud = Cloud(tmp_path, [NAME])
    cloud.folder[NAME].write_bytes(b"not a zip")
    monkeypatch.setattr(remarkable, "rmapi", cloud)
    with pytest.raises(SystemExit, match="not a zip archive"):
        remarkable.pull(["svgd"])
    assert NAME in cloud.folder
    assert not (lib / "annotations").exists()


def test_pull_refuses_documents_not_in_the_library(lib, tmp_path, monkeypatch):
    cloud = Cloud(tmp_path, ["Renamed on tablet"])
    monkeypatch.setattr(remarkable, "rmapi", cloud)
    with pytest.raises(SystemExit, match="not a library PDF"):
        remarkable.pull(["renamed"])
    assert "Renamed on tablet" in cloud.folder


def test_annotated_folders_are_never_chapter_folders():
    assert {"annotated", "annotations"} <= ingest.RESERVED
