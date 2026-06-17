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
    # shim via the venv python. Honors TOKEN_USAGE_DRY_RUN /
    # TOKEN_USAGE_HOSTNAME / LANGFUSE_* from the user's env (no .env
    # required on this install path — brew users typically wire up env via
    # launchd/systemd).
    (bin/"token-usage").write <<~SH
      #!/bin/bash
      set -euo pipefail
      export PATH="#{libexec}/node_modules/bin:$PATH"
      exec "#{venv}/bin/python" "#{libexec}/shim/ccusage-ship.py" "$@"
    SH
    chmod 0755, bin/"token-usage"
  end

  def caveats
    <<~EOS
      token-usage requires these env vars at runtime (see README):
        LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, CCUSAGE_SOURCES

      Put them in a durable .env that survives `brew upgrade` (the Cellar copy
      does NOT) and is read from any directory:
        ~/.config/token-usage/.env

      Pinned ccusage version: see #{opt_libexec}/CCUSAGE_VERSION

      Test / backfill from anywhere:
        token-usage --dry-run                 # print, don't send
        token-usage --since-days 300          # one-off backfill of 300 days
        token-usage --since 2026-01-01        # backfill from an exact date
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
