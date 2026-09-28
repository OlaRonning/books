# Home Manager module: `programs.books`.
#
# Every host gets the `books` and `books-rofi` commands. The hub (the one host
# that writes the library, catalog and index) also gets a systemd path unit
# that ingests new PDFs as they appear, plus a nightly sweep.
{
  config,
  lib,
  pkgs,
  osConfig ? null,
  ...
}:
let
  cfg = config.programs.books;
  hostName = if osConfig != null then osConfig.networking.hostName else null;
in
{
  options.programs.books = {
    enable = lib.mkEnableOption "books, full-text search over a PDF library";

    library = lib.mkOption {
      type = lib.types.str;
      default = "${config.home.homeDirectory}/books";
      description = "Library directory: PDFs, catalog.toml and the index.";
    };

    hub = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "workstation";
      description = ''
        Hostname of the single machine that writes the library, catalog and
        index. Other hosts only read (and queue new PDFs with `books add`).
        Null means this is a single-machine setup and any host may write.
      '';
    };

    ingest = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = cfg.hub == null || cfg.hub == hostName;
        defaultText = lib.literalExpression "hub == null || hub == osConfig.networking.hostName";
        description = "Watch the library and ingest new PDFs on this host.";
      };
      schedule = lib.mkOption {
        type = lib.types.str;
        default = "03:30";
        description = "systemd calendar time for the fallback sweep.";
      };
    };

    package = lib.mkOption {
      type = lib.types.package;
      readOnly = true;
      description = "The configured books package.";
    };
  };

  config = lib.mkIf cfg.enable (
    lib.mkMerge [
      {
        programs.books.package = pkgs.callPackage ./package.nix {
          withIngest = cfg.ingest.enable;
          inherit (cfg) library hub;
        };
        home.packages = [ cfg.package ];
      }

      (lib.mkIf cfg.ingest.enable {
        systemd.user.services.books-process = {
          Unit.Description = "Ingest new PDFs into ${cfg.library} and update the index";
          Service = {
            Type = "oneshot";
            ExecStart = "${lib.getExe cfg.package} process";
            Nice = 10;
            IOSchedulingClass = "idle";
          };
        };

        # New PDFs appear by rename (local copies, Syncthing deliveries). Runs
        # that find nothing new write nothing, so they cannot retrigger.
        systemd.user.paths.books-process = {
          Unit.Description = "Watch ${cfg.library} for new PDFs";
          Path = {
            PathChanged = [
              cfg.library
              "${cfg.library}/inbox"
              "${cfg.library}/pdfs"
            ];
            MakeDirectory = true;
          };
          Install.WantedBy = [ "paths.target" ];
        };

        systemd.user.timers.books-process = {
          Unit.Description = "Fallback sweep: ingest and index ${cfg.library}";
          Timer = {
            OnCalendar = cfg.ingest.schedule;
            Persistent = true;
          };
          Install.WantedBy = [ "timers.target" ];
        };
      })
    ]
  );
}
