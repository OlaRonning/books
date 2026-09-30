# books

Page-level full-text search and automatic cataloguing for a personal PDF
library.

- **Search** every page of every PDF, ranked by relevance (SQLite FTS5, BM25),
  from the terminal (`books QUERY`, picks with fzf) or rofi. Hits open in
  zathura at the matching page.
- **Catalog** in a hand-editable `catalog.toml`: authors, title, year, type,
  edition, venue and tags. Filter searches by tag or type.
- **Ingest** new PDFs automatically: merge chapter folders into one PDF with
  bookmarks, OCR scans, identify the work (DOI via Crossref, arXiv, ISBN via
  Open Library, else a Crossref title search; Claude as fallback and for
  tags), rename it `Author - Title (Year).pdf`, and catalogue and index it.
  Every lookup result is checked against the PDF's opening pages before it is
  trusted, so a DOI from a paper's reference list cannot misname it.
- **Duplicates** are caught on ingest (identical file, shared DOI/arXiv/ISBN,
  or same first author and near-identical title) and parked in
  `inbox/duplicates/`; nothing is deleted or replaced automatically.
- **Review** what ingest could not settle with `books review`. Each item
  shows its problem (recorded at ingest) and opens the PDF; then accept, edit
  (in `$EDITOR`, renaming the PDF to match), re-identify or delete flagged
  entries; keep, replace or keep-both for duplicates; retry failed PDFs. It
  works on any machine and only edits the catalog and moves files; anything
  to re-ingest goes back through `inbox/` to the hub.
- **Multi-machine**: one hub writes; other machines read a synced copy of the
  library (e.g. with Syncthing) and queue new PDFs with `books add`.

## Library layout

    ~/books/
      pdfs/                  one PDF per work: Author - Title (Year[, edition]).pdf
      inbox/                 drop PDFs or chapter folders here (or in ~/books itself)
      annotations/           reMarkable archives of pulled readings (<name>.rmdoc)
      annotated/             annotated copies and highlights of those readings
      catalog.toml           one [[work]] per PDF in pdfs/
      .books-index.sqlite    the search index (built on the hub)

The library is never part of this repository.

### catalog.toml

    [[work]]
    file = "Nocedal & Wright - Numerical Optimization (2006, 2nd ed).pdf"  # in pdfs/
    type = "book"                  # book | article | notes
    authors = ["Jorge Nocedal", "Stephen Wright"]
    editors = true                 # optional: authors are editors
    title = "Numerical Optimization"
    year = 2006                    # of the copy held
    edition = "2nd"                # optional
    venue = "..."                  # optional, articles
    doi = "..."                    # optional identifiers, set only when a lookup
    arxiv = "..."                  #   was verified against the PDF; used for
    isbn = "..."                   #   duplicate detection
    tags = ["optimization"]
    review = "no DOI, ..."         # set when identified without a lookup: why

## Usage

    books QUERY...               search pages; pick a hit to open it
    books -t TAG --type article QUERY...
    books ls [-t TAG]            browse the catalog
    books tags | books stats
    books add FILE|FOLDER...     queue for ingest
    books process                ingest the inbox and index (hub)
    books redo FILE...           re-identify catalogued PDFs (hub)
    books review                 work through flagged entries, parked duplicates
                                 and failed ingests (fzf; o/a/e/r/d per item)
    books dupes [--backfill]     list suspected duplicates; --backfill first adds
                                 verified identifiers to existing entries (hub)
    books index [--full]         rebuild the index (hub)
    books push [QUERY...]        send a PDF to the reMarkable
    books pull [--keep] [QUERY...]
                                 bring it back with its annotations (hub)
    books tablet                 list what is on the reMarkable

rofi: `rofi -modi books:books-rofi -show books`. Typing filters the catalog;
Enter on unmatched text searches every page; `t:TAG words` narrows by tag.

## reMarkable

Reading on a reMarkable goes through its cloud, with
[rmapi](https://github.com/ddvk/rmapi). Pair it once on each machine: run
`rmapi` and enter the code from my.remarkable.com/device/browser/connect.

- `books push QUERY` uploads the chosen PDF to `/Books` on the tablet
  (`BOOKS_TABLET_FOLDER` changes that), named after its file. Any machine.
- `books pull QUERY` brings it back (hub only) and deletes it from the tablet
  (`--keep` leaves it there). The original in `pdfs/` is never touched:
  - `annotations/<name>.rmdoc` is the tablet's own archive of the document,
    every stroke and highlight included. It is the lossless copy: pushing the
    PDF again uploads this instead, so the annotations come back.
  - `annotated/<name>.pdf` is a rendered copy with the annotations drawn in
    (rmapi's renderer, which is basic). Search results and `books ls` open it
    in place of the original.
  - `annotated/<name>.md` lists the highlighted passages by PDF page.

The tablet copy is only deleted once the downloaded archive checks out.
Documents are matched to the library by name, so a document renamed on the
tablet is left alone.

## Install (Home Manager, flakes)

```nix
# flake.nix
inputs.books.url = "github:OlaRonning/books";

# home-manager configuration
imports = [ inputs.books.homeManagerModules.default ];
programs.books = {
  enable = true;
  library = "/home/me/books";   # default: ~/books
  hub = "workstation";         # omit on a single machine
};
```

The hub gets a systemd user path unit that ingests new PDFs as they appear,
and a nightly fallback sweep. `claude-code` is taken from your package set
(it is unfree), and is only needed on the hub.

Standalone: `nix run github:OlaRonning/books -- QUERY`.

## Privacy

Ingest sends identifiers (DOI, arXiv id, ISBN) to Crossref, arXiv and Open
Library, and the first pages of each new PDF (up to ~12k characters) to
Claude. Search and indexing are local. `books push` and `pull` go through
the reMarkable cloud.

## Development

    nix develop
    pytest && ruff check books tests && pyright
