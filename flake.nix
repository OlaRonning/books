{
  description = "books: page-level full-text search and automatic cataloguing for a PDF library";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems =
        f:
        nixpkgs.lib.genAttrs systems (
          system:
          f (
            import nixpkgs {
              inherit system;
              config.allowUnfree = true; # claude-code, for the ingest build
            }
          )
        );
    in
    {
      packages = forAllSystems (pkgs: rec {
        books = pkgs.callPackage ./nix/package.nix { };
        books-ingest = books.override { withIngest = true; };
        default = books;
      });

      # Builds run the test suite (pytestCheckHook).
      checks = forAllSystems (
        pkgs:
        let
          inherit (self.packages.${pkgs.stdenv.hostPlatform.system}) books books-ingest;
        in
        {
          inherit books books-ingest;
          # The unconfigured package must not bake in a library or hub.
          wrapper-defaults = pkgs.runCommand "books-wrapper-defaults" { } ''
            if grep -E 'BOOKS_(DIR|HUB)' ${books}/bin/books; then
              echo "default package sets BOOKS_DIR/BOOKS_HUB" >&2; exit 1
            fi
            touch $out
          '';
        }
      );

      homeManagerModules.default = import ./nix/hm-module.nix;

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (p: [
              p.pytest
              p.pikepdf
            ]))
            pkgs.poppler-utils
            pkgs.pyright
            pkgs.ruff
          ];
        };
      });
    };
}
