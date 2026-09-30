"""Opening results: fzf for picking in a terminal, zathura for reading."""

import subprocess

from . import config


def open_pdf(path, page):
    """Open a PDF (a name in pdfs/, or an absolute path) in zathura; returns
    the process so callers can close it again. A name with an annotated copy
    (pulled back from the reMarkable) opens that copy."""
    annotated = config.annotated_path() / path
    return subprocess.Popen(
        ["zathura", f"--page={page}", str(annotated if annotated.is_file() else config.pdfs_path() / path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )


def pick(lines, prompt):
    """lines are 'file<TAB>page<TAB>display'; the chosen (file, page), or None."""
    chosen = subprocess.run(
        ["fzf", "--ansi", "--delimiter", "\t", "--with-nth", "3", "--no-sort",
         "--prompt", prompt + "> "],
        input="\n".join(lines), capture_output=True, text=True,
        check=False,  # fzf exits non-zero when nothing is picked
    ).stdout.strip()
    if not chosen:
        return None
    path, page, _ = chosen.split("\t", 2)
    return path, page


def pick_and_open(lines, prompt):
    chosen = pick(lines, prompt)
    if chosen:
        open_pdf(*chosen)
