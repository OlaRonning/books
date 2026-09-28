"""`books review`: work through what ingest could not settle on its own.

  review   catalog entries marked review = true (identified without a lookup)
  dup      PDFs parked in inbox/duplicates/, with the entry they matched
  failed   PDFs parked in inbox/failed/, with the error

Review runs on any machine. It only edits catalog.toml and moves files; when
something needs ingesting again it goes back to inbox/, where the hub picks
it up (so only the hub ever ingests or indexes).
"""

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import catalog, config, ingest
from .viewer import open_pdf


def why(pdf):
    note = pdf.with_name(pdf.name + ingest.WHY_SUFFIX)
    return note.read_text().strip() if note.exists() else ""


def parked(kind):
    folder = config.inbox_path() / kind
    return sorted(folder.glob("*.pdf")) if folder.is_dir() else []


NO_REASON = "reason not recorded (ingested before reasons were kept; [r]edo records one)"


def problem(work):
    """The review reason stored at ingest (older entries only say `true`)."""
    return work["review"] if isinstance(work.get("review"), str) else NO_REASON


def missing(work):
    """Useful fields an entry lacks, as a hint for editing."""
    gaps = [k for k in ("year", "authors") if not work.get(k)]
    if not any(work.get(k) for k in ("doi", "arxiv", "isbn")):
        gaps.append("doi/arxiv/isbn")
    if work.get("type") == "article" and not work.get("venue"):
        gaps.append("venue")
    return gaps


def items():
    """Everything awaiting a decision, as (kind, name, description, problem)."""
    out = [("review", f, catalog.label(w), problem(w))
           for f, w in sorted(catalog.load().items()) if w.get("review")]
    for pdf in parked("duplicates"):
        existing, _, reason = why(pdf).partition("\t")
        out.append(("dup", pdf.name, f"{pdf.name}  ≈ {existing}",
                    f"{reason or 'duplicate'} of {existing or 'a catalogued work'}"))
    for pdf in parked("failed"):
        out.append(("failed", pdf.name, pdf.name, why(pdf) or "ingest failed"))
    return out


def _drop_parked(pdf):
    pdf.unlink(missing_ok=True)
    pdf.with_name(pdf.name + ingest.WHY_SUFFIX).unlink(missing_ok=True)


def _to_inbox(pdf):
    """Move a PDF into inbox/ (a free name), dropping its .why note."""
    dest = config.inbox_path() / pdf.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    stem, n = dest.stem, 2
    while dest.exists():
        dest, n = dest.with_name(f"{stem} [{n}].pdf"), n + 1
    shutil.move(pdf, dest)
    pdf.with_name(pdf.name + ingest.WHY_SUFFIX).unlink(missing_ok=True)
    return dest


# --- actions on flagged entries ------------------------------------------------

def accept(file):
    """The entry is right as it is: clear its review flag."""
    return catalog.remove_key(file, "review")


def redo(file):
    """Re-identify from scratch: drop the entry, send the PDF to the inbox."""
    catalog.remove_entries({file})
    return _to_inbox(config.pdfs_path() / file)


def delete(file):
    catalog.remove_entries({file})
    (config.pdfs_path() / file).unlink(missing_ok=True)


def apply_edit(file, text):
    """Store an edited entry; rename the PDF if the metadata implies a new
    name. The edit counts as a review, so the flag is cleared. Returns the
    (possibly new) file name."""
    work = catalog.parse_entry(text)
    shelf = config.pdfs_path()
    new = ingest.base_name(work)
    if new != file:
        new = ingest.file_name(work, shelf)
        (shelf / file).rename(shelf / new)
        text = text.replace(catalog.toml_str(work["file"]), catalog.toml_str(new), 1)
    catalog.replace_entry(file, text)
    catalog.remove_key(new, "review")
    return new


def edit(file):
    text = catalog.entry_block(file)
    if text is None:
        raise KeyError(file)
    editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
    with tempfile.NamedTemporaryFile("w+", suffix=".toml", delete=False) as fh:
        fh.write(text)
    tmp = Path(fh.name)
    try:
        while True:
            subprocess.run([*editor, str(tmp)], check=False)
            edited = tmp.read_text()
            if edited == text:
                return file  # unchanged: nothing to do
            try:
                return apply_edit(file, edited)
            except (ValueError, KeyError) as err:  # invalid TOML or missing fields
                if input(f"invalid entry ({err}); edit again? [Y/n] ").strip().lower() == "n":
                    return file
    finally:
        tmp.unlink(missing_ok=True)


# --- actions on parked PDFs ------------------------------------------------------

def keep_existing(name):
    """A true duplicate: discard the parked copy."""
    _drop_parked(config.inbox_path() / "duplicates" / name)


def replace_existing(name):
    """The parked copy is better: drop the catalogued one, ingest this."""
    pdf = config.inbox_path() / "duplicates" / name
    existing = why(pdf).partition("\t")[0]
    if existing:
        delete(existing)
    return _to_inbox(pdf)


def keep_both(name):
    """Not really the same work: ingest it without the duplicate check."""
    dest = _to_inbox(config.inbox_path() / "duplicates" / name)
    ingest.keep_marker(dest).touch()
    return dest


def retry(name):
    return _to_inbox(config.inbox_path() / "failed" / name)


def discard_failed(name):
    _drop_parked(config.inbox_path() / "failed" / name)


# --- interactive loop ---------------------------------------------------------------

PARKED_IN = {"dup": "duplicates", "failed": "failed"}
MENUS = {
    "review": "[a]ccept [e]dit [r]edo [d]elete [o]pen again [s]kip",
    "dup": "[k]eep existing [p] replace existing [b]oth [o]pen again [s]kip",
    "failed": "[r]etry [d]elete [o]pen again [s]kip",
}


def pick(entries):
    """fzf over the items; the preview pane shows each one's problem."""
    lines = [f"{kind}\t{name}\t{kind:<7} {desc}\t{why_}" for kind, name, desc, why_ in entries]
    chosen = subprocess.run(
        ["fzf", "--delimiter", "\t", "--with-nth", "3", "--prompt", "review> ", "--no-sort",
         "--preview", "echo {4}", "--preview-window", "down,3,wrap", "--preview-label", " problem "],
        input="\n".join(lines), capture_output=True, text=True, check=False,  # Esc: rc 130
    ).stdout.strip()
    return chosen.split("\t", 2)[:2] if chosen else None


RED, BOLD, DIM, OFF = "\033[1;31m", "\033[1m", "\033[2m", "\033[0m"


def show(kind, name, pdf):
    """Print the problem first, highlighted, then the details."""
    if kind == "review":
        work = catalog.load().get(name, {})
        print(f"\n{RED}problem:{OFF} {problem(work)}")
        gaps = missing(work)
        if gaps:
            print(f"{BOLD}missing:{OFF} {', '.join(gaps)}")
        print(f"{DIM}{catalog.entry_block(name) or name}{OFF}")
    else:
        existing, _, reason = why(pdf).partition("\t") if kind == "dup" else ("", "", why(pdf))
        detail = f"{reason or 'duplicate'} of {existing}" if kind == "dup" else reason or "ingest failed"
        print(f"\n{RED}problem:{OFF} {detail}\n{DIM}{name}{OFF}")


def open_for(kind, name, pdf):
    """Open what the decision is about; returns the viewer processes."""
    if kind == "review":
        return [open_pdf(name, 1)]
    viewers = [open_pdf(str(pdf), 1)]
    existing = why(pdf).partition("\t")[0] if kind == "dup" else ""
    if existing and (config.pdfs_path() / existing).exists():
        viewers.append(open_pdf(existing, 1))  # side by side with the catalogued copy
    return viewers


def close(viewers):
    for v in viewers:
        if v.poll() is None:
            v.terminate()


def confirm(question):
    return input(f"{question} [y/N] ").strip().lower() == "y"


def act(kind, name):
    """Show one item with its PDF open and run the chosen action; the viewers
    close again afterwards. Returns a status line."""
    pdf = config.inbox_path() / PARKED_IN[kind] / name if kind in PARKED_IN else None
    viewers = open_for(kind, name, pdf)
    try:
        show(kind, name, pdf)
        while True:
            key = input(MENUS[kind] + ": ").strip().lower()[:1]
            if key != "o":
                break
            close(viewers)
            viewers = open_for(kind, name, pdf)
        return decide(kind, name, pdf, key)
    finally:
        close(viewers)


def decide(kind, name, pdf, key):
    existing = why(pdf).partition("\t")[0] if kind == "dup" and pdf else ""
    match kind, key:
        case "review", "a":
            accept(name)
            return f"accepted {name}"
        case "review", "e":
            return f"saved {edit(name)}"
        case "review", "r":
            redo(name)
            return f"re-queued {name}"
        case "review", "d" if confirm(f"delete {name} and its entry?"):
            delete(name)
            return f"deleted {name}"
        case "dup", "k" if confirm(f"discard parked {name}?"):
            keep_existing(name)
            return f"discarded {name}"
        case "dup", "p" if confirm(f"replace {existing or 'the existing copy'} with {name}?"):
            replace_existing(name)
            return f"queued {name} to replace the existing copy"
        case "dup", "b":
            keep_both(name)
            return f"queued {name} as a separate work"
        case "failed", "r":
            retry(name)
            return f"re-queued {name}"
        case "failed", "d" if confirm(f"delete {name}?"):
            discard_failed(name)
            return f"deleted {name}"
    return ""


def main():
    status = ""
    while True:
        entries = items()
        if not entries:
            print(status or "nothing to review")
            return
        if status:
            print(status)
        chosen = pick(entries)
        if not chosen:
            return
        try:
            status = act(*chosen)
        except (OSError, KeyError, ValueError) as err:
            print(f"error: {err}", file=sys.stderr)
            status = ""
