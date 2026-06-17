{
  description = "token-usage — ship ccusage daily aggregates to Langfuse";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        ccusageVersion = builtins.replaceStrings [ "\n" ] [ "" ]
          (builtins.readFile ./CCUSAGE_VERSION);

        # Python package for the shim. buildPythonApplication keeps it
        # self-contained and exposes a `ccusage-ship` console script.
        tokenUsageShim = pkgs.python3Packages.buildPythonApplication {
          pname = "token-usage-shim";
          version = ccusageVersion;
          format = "other";  # no setup.py — we just copy the script

          src = ./.;

          propagatedBuildInputs = with pkgs.python3Packages; [
            requests
            python-dotenv
          ];

          dontBuild = true;

          # The installed file needs an executable shebang because we expose
          # it as a console script. Plain `cp` would leave it without one,
          # so `nix run` would have to invoke python explicitly. patchShebangs
          # rewrites #!/usr/bin/env python3 to the Nix-store python so the
          # subprocess shim invocation works on any host.
          installPhase = ''
            runHook preInstall
            mkdir -p $out/bin $out/share/token-usage
            cp shim/ccusage-ship.py $out/bin/ccusage-ship
            sed -i '1i#!/usr/bin/env python3' $out/bin/ccusage-ship
            chmod 0755 $out/bin/ccusage-ship
            cp CCUSAGE_VERSION $out/share/token-usage/CCUSAGE_VERSION
            runHook postInstall
          '';
        };

        # Real `ccusage` binary on PATH so the shim's
        # `subprocess.check_output(["ccusage", ...])` finds it.
        # Earlier draft used a bash function exported with `export -f` which
        # is NOT inherited by Python subprocesses (only by other bash
        # subshells) — Codex caught it. Delegates to npx-with-pinned-version
        # under the hood.
        ccusageWrapper = pkgs.writeShellScriptBin "ccusage" ''
          set -euo pipefail
          exec ${pkgs.nodejs_20}/bin/npx --yes "ccusage@${ccusageVersion}" "$@"
        '';

        # Top-level entry point. Adds ccusage to PATH and runs the shim
        # console script.
        tokenUsage = pkgs.writeShellApplication {
          name = "token-usage";
          runtimeInputs = [ pkgs.nodejs_20 ccusageWrapper tokenUsageShim ];
          text = ''
            exec ccusage-ship "$@"
          '';
        };
      in {
        packages.default = tokenUsage;
        packages.token-usage = tokenUsage;
        packages.token-usage-shim = tokenUsageShim;

        apps.default = {
          type = "app";
          program = "${tokenUsage}/bin/token-usage";
        };

        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            python3
            python3Packages.requests
            python3Packages.python-dotenv
            nodejs_20
          ];
          shellHook = ''
            echo "token-usage dev shell — pinned ccusage version: ${ccusageVersion}"
            echo "Run ccusage via: npx --yes ccusage@${ccusageVersion} <source> daily --json"
          '';
        };
      });
}
