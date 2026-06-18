# token-usage Homebrew formula.
#
# Lives in this repo as a TEMPLATE. The release workflow substitutes
# __VERSION__ and __SHA256__ and PRs the resulting file into
# FactusConsulting/homebrew-tap (NOT homebrew-tools).
#
# Works on both macOS and Linux (brew on Linux).
#
# Public release: the tarball is fetched anonymously from the GitHub release
# assets — no token needed.
class TokenUsage < Formula
  desc "Ship ccusage daily aggregates to a self-hosted Langfuse instance"
  homepage "https://github.com/FactusConsulting/token-usage"
  url "https://github.com/FactusConsulting/token-usage/releases/download/v__VERSION__/token-usage-__VERSION__.tar.gz"
  sha256 "__SHA256__"
  license "MIT"
  version "__VERSION__"

  depends_on "node"
  depends_on "python@3.12"

  def install
    # Stash the whole source tree under libexec so installers/ and shim/ are
    # together (the wrapper invokes the shim by path).
    libexec.install Dir["*"]

    ccusage_version = (libexec/"CCUSAGE_VERSION").read.strip

    # Install ccusage globally pinned to the version from CCUSAGE_VERSION.
    # Done at install time so first run isn't network-bound. Uses the node
    # we depend_on, not whatever the user already has.
    system "#{Formula["node"].opt_bin}/npm", "install", "-g",
           "--prefix=#{libexec}/node_modules",
           "ccusage@#{ccusage_version}"

    # Python venv with the shim's runtime deps. The earlier draft invoked
    # bare python3.12 against the shim, but the shim imports `requests`
    # unconditionally — Codex caught that as `ModuleNotFoundError` on a
    # clean install. Putting requirements.txt into a venv under libexec
    # keeps the formula self-contained without polluting the user's
    # site-packages.
    py = Formula["python@3.12"].opt_bin/"python3.12"
    venv = libexec/"venv"
    system py, "-m", "venv", venv
    system venv/"bin/pip", "install", "--quiet", "--upgrade", "pip"
    system venv/"bin/pip", "install", "--quiet",
           "-r", libexec/"shim/requirements.txt"

    # token-usage wrapper: prepend our pinned ccusage to PATH, then run the
    # shim via the venv python. The shim reads its config from
    # ~/.config/token-usage/.env (XDG-aware), so the wrapper needs no env of
    # its own — that's also what the brew service below relies on.
    (bin/"token-usage").write <<~SH
      #!/bin/bash
      set -euo pipefail
      export PATH="#{libexec}/node_modules/bin:$PATH"
      exec "#{venv}/bin/python" "#{libexec}/shim/ccusage-ship.py" "$@"
    SH
    chmod 0755, bin/"token-usage"
  end

  # `brew services start token-usage` registers an hourly importer (a systemd
  # timer on Linux, launchd on macOS) — no repo clone, no hand-written unit.
  # The run reads ~/.config/token-usage/.env for everything, including
  # CCUSAGE_SINCE_DAYS (how many days back each run ships, default 14).
  service do
    run [opt_bin/"token-usage"]
    run_type :interval
    interval 3600
    log_path var/"log/token-usage.log"
    error_log_path var/"log/token-usage.log"
  end

  def caveats
    <<~EOS
      1) Create a durable config (survives `brew upgrade`, read from any dir):

           mkdir -p ~/.config/token-usage
           cp #{opt_libexec}/shim/.env.example ~/.config/token-usage/.env
           "${EDITOR:-nano}" ~/.config/token-usage/.env

         Fill in LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY /
         CCUSAGE_SOURCES. Set TOKEN_USAGE_HOSTNAME if this machine shares a
         hostname with another install (e.g. WSL2 vs its Windows host), and
         CCUSAGE_SINCE_DAYS for how many days each run backfills (default 14).

      2) Start the hourly importer:

           brew services start token-usage

      Run manually / one-off backfill from anywhere:
           token-usage --dry-run                 # print, don't send
           token-usage --since-days 300          # backfill 300 days
           token-usage --since 2026-01-01        # backfill from an exact date

      Pinned ccusage version: see #{opt_libexec}/CCUSAGE_VERSION
    EOS
  end

  test do
    assert_path_exists libexec/"shim/ccusage-ship.py"
    assert_path_exists libexec/"CCUSAGE_VERSION"
    assert_path_exists libexec/"node_modules/bin/ccusage"
    # Shim exits 1 with no env vars set — that proves it ran (vs. crashing on import).
    output = shell_output("#{bin}/token-usage 2>&1", 1)
    assert_match "LANGFUSE", output
  end
end
