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

          installPhase = ''
            runHook preInstall
            mkdir -p $out/bin $out/share/token-usage
            install -m 0755 shim/ccusage-ship.py $out/bin/ccusage-ship
            cp CCUSAGE_VERSION $out/share/token-usage/CCUSAGE_VERSION
            runHook postInstall
          '';
        };

        # Wrapper: ensures node + a pinned ccusage are on PATH, then runs
        # the shim. ccusage is fetched via `npx` at first run (no nixpkgs
        # entry for ccusage exists; npx caches under XDG_CACHE_HOME).
        tokenUsage = pkgs.writeShellApplication {
          name = "token-usage";
          runtimeInputs = [ pkgs.nodejs_20 tokenUsageShim ];
          text = ''
            set -euo pipefail
            CCUSAGE_PINNED="$(cat "${tokenUsageShim}/share/token-usage/CCUSAGE_VERSION")"
            export PATH="$(npm config get prefix 2>/dev/null || echo "$HOME/.npm-global")/bin:$PATH"
            # Use npx with the pinned version. --yes silences the prompt on first
            # run; npx caches the package so the second call is fast.
            ccusage() { npx --yes "ccusage@$CCUSAGE_PINNED" "$@"; }
            export -f ccusage
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
