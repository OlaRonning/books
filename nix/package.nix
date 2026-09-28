{
  lib,
  python3Packages,
  poppler-utils,
  fzf,
  zathura,
  ocrmypdf,
  claude-code,
  # Hub build: merge chapter folders, OCR, and ask Claude when ingesting.
  withIngest ? false,
  # Defaults baked into the wrapper; the environment still overrides them.
  library ? null,
  hub ? null,
}:

python3Packages.buildPythonApplication {
  pname = "books";
  version = "0.1.0";
  pyproject = true;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../books
      ../tests
      ../pyproject.toml
      ../README.md
    ];
  };

  build-system = [ python3Packages.setuptools ];
  dependencies = lib.optionals withIngest [ python3Packages.pikepdf ];

  nativeCheckInputs = [
    python3Packages.pytestCheckHook
    python3Packages.pikepdf
  ];

  makeWrapperArgs = [
    # pdftotext/pdfinfo are required; the viewers are the user's if they have them.
    "--prefix PATH : ${lib.makeBinPath [ poppler-utils ]}"
    "--suffix PATH : ${
      lib.makeBinPath (
        [
          fzf
          zathura
        ]
        ++ lib.optionals withIngest [
          ocrmypdf
          claude-code
        ]
      )
    }"
  ]
  ++ lib.optional (library != null) "--set-default BOOKS_DIR ${lib.escapeShellArg library}"
  ++ lib.optional (hub != null) "--set-default BOOKS_HUB ${lib.escapeShellArg hub}";

  meta = {
    description = "Page-level full-text search and automatic cataloguing for a PDF library";
    mainProgram = "books";
    platforms = lib.platforms.linux;
  };
}
