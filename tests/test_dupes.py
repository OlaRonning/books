import pytest

from books import dupes


@pytest.mark.parametrize(("key", "a", "b"), [
    ("doi", "10.1109/TRO.2016.2544304", "https://doi.org/10.1109/tro.2016.2544304"),
    ("arxiv", "2406.15742v2", "arXiv:2406.15742"),
    ("isbn", "0387303030", "978-0387-30303-1"),
])
def test_identifier_normalisation(key, a, b):
    assert dupes.norm_id(key, a) == dupes.norm_id(key, b)


def test_same_work_by_identifier():
    assert dupes.same_work({"arxiv": "1608.04471v3"}, {"arxiv": "1608.04471"}) == "same arxiv"


def test_same_work_by_author_and_title():
    a = {"authors": ["Qiang Liu", "Dilin Wang"],
         "title": "Stein Variational Gradient Descent: A General Purpose Bayesian Inference Algorithm"}
    b = {"authors": ["Q. Liu"],
         "title": "Stein variational gradient descent - a general purpose Bayesian inference algorithm"}
    assert dupes.same_work(a, b) == "same first author and title"


def test_different_works_do_not_match():
    a = {"authors": ["Luca Carlone"], "title": "Lagrangian Duality in 3D SLAM"}
    b = {"authors": ["Luca Carlone"], "title": "Duality-based Verification Techniques for 2D SLAM"}
    assert dupes.same_work(a, b) is None
    assert dupes.same_work({"title": "X"}, {"title": "X"}) is None  # no author: no title match


def test_report_groups_identical_files_and_same_works():
    works = {"a.pdf": {"doi": "10.1/x"}, "b.pdf": {"doi": "10.1/X"}, "c.pdf": {}, "d.pdf": {}}
    hashes = {"a.pdf": "h1", "b.pdf": "h2", "c.pdf": "h3", "d.pdf": "h3"}
    assert sorted(dupes.report(works, hashes)) == [
        ("a.pdf", "b.pdf", "same doi"), ("c.pdf", "d.pdf", "identical file")]
