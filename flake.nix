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

        # ccusage itself: the static binary upstream publishes per platform as
        # the npm package @ccusage/ccusage-<platform>, pinned with its sha512
        # in ccusage.lock.json. No Node, no npx download at run time, and not
        # nixpkgs' ccusage, which lags upstream by weeks. The flake uses the
        # exact version every other channel ships. scripts/ccusage_pin.py
        # writes the lock file.
        ccusageLock = builtins.fromJSON (builtins.readFile ./ccusage.lock.json);
        npmPlatform = {
          x86_64-linux = "linux-x64";
          aarch64-linux = "linux-arm64";
          x86_64-darwin = "darwin-x64";
          aarch64-darwin = "darwin-arm64";
        }.${system} or (throw "token-usage: no ccusage binary for ${system}");
        ccusageBinary = ccusageLock.platforms.${npmPlatform};

        ccusage = pkgs.stdenvNoCC.mkDerivation {
          pname = "ccusage";
          version = ccusageLock.version;
          src = pkgs.fetchurl {
            inherit (ccusageBinary) url;
            hash = ccusageBinary.integrity;
          };
          sourceRoot = "package";
          # Upstream's release binary: static on Linux, signed on macOS.
          # Leave it exactly as published.
          dontStrip = true;
          dontPatchELF = true;
          installPhase = ''
            runHook preInstall
            install -Dm755 bin/ccusage $out/bin/ccusage
            runHook postInstall
          '';
          doInstallCheck = true;
          installCheckPhase = ''
            $out/bin/ccusage --version | grep -qw "${ccusageLock.version}"
          '';
          meta = {
            description = "Usage analysis for AI coding CLIs (pinned upstream binary)";
            homepage = "https://github.com/ccusage/ccusage";
            mainProgram = "ccusage";
          };
        };

        # Top-level entry point. TOKEN_USAGE_CCUSAGE points the shim at the
        # pinned ccusage by absolute path, so a different ccusage earlier on
        # the user's PATH cannot take its place.
        tokenUsage = pkgs.writeShellApplication {
          name = "token-usage";
          runtimeInputs = [ ccusage tokenUsageShim ];
          text = ''
            export TOKEN_USAGE_CCUSAGE=${ccusage}/bin/ccusage
            exec ccusage-ship "$@"
          '';
        };
      in {
        packages.default = tokenUsage;
        packages.token-usage = tokenUsage;
        packages.token-usage-shim = tokenUsageShim;
        packages.ccusage = ccusage;

        apps.default = {
          type = "app";
          program = "${tokenUsage}/bin/token-usage";
        };

        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            python3
            python3Packages.requests
            python3Packages.python-dotenv
          ] ++ [ ccusage ];
          shellHook = ''
            echo "token-usage dev shell — pinned ccusage version: ${ccusageVersion}"
            echo "ccusage ${ccusageVersion} is on PATH: ccusage <source> session --json"
          '';
        };
      });
}
