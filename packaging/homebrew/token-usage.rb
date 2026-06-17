# token-usage Homebrew formula.
#
# Lives in this repo as a TEMPLATE. The release workflow substitutes
# __VERSION__ and __SHA256__ and PRs the resulting file into
# FactusConsulting/homebrew-tap (NOT homebrew-tools).
#
# Works on both macOS and Linux (brew on Linux).
#
# Private-repo note: brew needs `HOMEBREW_GITHUB_API_TOKEN` exported with
# `repo` read scope so it can fetch the private release tarball.
class TokenUsage < Formula
  desc "Ship ccusage daily aggregates to a self-hosted Langfuse instance"
  homepage "https://github.com/FactusConsulting/token-usage"
  url "https://github.com/FactusConsulting/token-usage/releases/download/v__VERSION__/token-usage-__VERSION__.tar.gz"
  sha256 "__SHA256__"
  license :cannot_represent  # private; not OSI-licensed
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

    py = Formula["python@3.12"].opt_bin/"python3.12"
    ccusage_bin = libexec/"node_modules/bin/ccusage"

    # token-usage wrapper: prepend our pinned ccusage to PATH, then run the
    # shim. Honors TOKEN_USAGE_DRY_RUN / TOKEN_USAGE_HOSTNAME / LANGFUSE_*
    # from the user's env (no .env required on this install path — brew users
    # typically wire up env via launchd/systemd).
    (bin/"token-usage").write <<~SH
      #!/bin/bash
      set -euo pipefail
      export PATH="#{libexec}/node_modules/bin:$PATH"
      exec "#{py}" "#{libexec}/shim/ccusage-ship.py" "$@"
    SH
    chmod 0755, bin/"token-usage"
  end

  def caveats
    <<~EOS
      token-usage requires these env vars at runtime (see README):
        LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, CCUSAGE_SOURCES

      Pinned ccusage version: see #{opt_libexec}/CCUSAGE_VERSION

      Test once with:
        TOKEN_USAGE_DRY_RUN=1 token-usage
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
