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
- **Multi-machine**: one hub writes; other machines read a synced copy of the
  library (e.g. with Syncthing) and queue new PDFs with `books add`.

## Library layout

    ~/books/
      pdfs/                  one PDF per work: Author - Title (Year[, edition]).pdf
      inbox/                 drop PDFs or chapter folders here (or in ~/books itself)
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
    review = true                  # set when identified without a lookup

## Usage

    books QUERY...               search pages; pick a hit to open it
    books -t TAG --type article QUERY...
    books ls [-t TAG]            browse the catalog
    books tags | books stats
    books add FILE|FOLDER...     queue for ingest
    books process                ingest the inbox and index (hub)
    books redo FILE...           re-identify catalogued PDFs (hub)
    books dupes [--backfill]     list suspected duplicates; --backfill first adds
                                 verified identifiers to existing entries (hub)
    books index [--full]         rebuild the index (hub)

rofi: `rofi -modi books:books-rofi -show books`. Typing filters the catalog;
Enter on unmatched text searches every page; `t:TAG words` narrows by tag.

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
Claude. Search and indexing are local.

## Development

    nix develop
    pytest && ruff check books tests && pyright
