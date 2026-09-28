"""Opening results: fzf for picking in a terminal, zathura for reading."""

import subprocess

from . import config


def open_pdf(path, page):
    subprocess.Popen(
        ["zathura", f"--page={page}", str(config.LIBRARY / path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )


def pick_and_open(lines, prompt):
    """lines are 'file<TAB>page<TAB>display'; open the chosen one."""
    chosen = subprocess.run(
        ["fzf", "--ansi", "--delimiter", "\t", "--with-nth", "3", "--no-sort",
         "--prompt", prompt + "> "],
        input="\n".join(lines), capture_output=True, text=True,
        check=False,  # fzf exits non-zero when nothing is picked
    ).stdout.strip()
    if chosen:
        path, page, _ = chosen.split("\t", 2)
        open_pdf(path, page)
