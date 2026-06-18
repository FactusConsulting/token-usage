# Agent Operating Procedures — token-usage

Authoritative reference for AI agents working in this repository.

## Repository purpose

Cross-machine LLM token-usage tracking. Per-PC installers + a shim that ships
`ccusage` aggregates to a self-hosted Langfuse instance (deployed via
`flux-home/applications/langfuse/`). See README for architecture.

## Provisioning a new machine (runbook)

Goal: install the package, point it at Langfuse, enable the hourly importer.
Repeat **per OS** — on a Windows box with WSL2 you do it twice (once in each),
with distinct hostnames, because Windows and WSL2 keep separate AI-CLI logs.

1. **Install** the package (each channel pins ccusage + bundles the shim):
   - Windows (Chocolatey): add source
     `https://factusconsulting.github.io/token-usage/chocolatey/index.json`,
     then `choco install token-usage -y` — registers the hourly Scheduled Task
     `TokenUsageCcusageShip`.
   - macOS / Linux / WSL2 (Homebrew): `brew tap FactusConsulting/tap && brew
     install token-usage`, then `brew services start token-usage` (systemd /
     launchd timer).
   - Nix: `nix run github:FactusConsulting/token-usage -- --help`.
2. **Configure** the `.env` (`~/.config/token-usage/.env`; Windows
   `%LOCALAPPDATA%\token-usage\.env`) from the shipped `.env.example`:
   ```ini
   LANGFUSE_HOST=https://langfuse.lwa.dk     # langfuse.factus.dk when off-LAN
   LANGFUSE_PUBLIC_KEY=pk-lf-...             # from the Langfuse project settings
   LANGFUSE_SECRET_KEY=sk-lf-...
   CCUSAGE_SOURCES=claude,codex             # add pi/copilot/gemini if used
   TOKEN_USAGE_HOSTNAME=<machine>-windows   # MUST be unique per OS install
   # CCUSAGE_GRANULARITY=session            # default; `daily` also supported
   # CCUSAGE_SINCE_DAYS=14                  # rolling window the timer ships
   ```
   On WSL2 use `<machine>-wsl2`. Keys come from the `token-usage` Langfuse
   project (org `factus-consulting`); the user supplies them — never commit them.
3. **Verify + backfill**:
   ```
   token-usage --dry-run          # prints the batch, sends nothing
   token-usage --since-days 300   # one-off backfill of all local history
   ```
4. **Confirm the timer** is enabled (`TokenUsageCcusageShip` is `Ready`, or
   `brew services list` shows it started). It re-ships the rolling
   `CCUSAGE_SINCE_DAYS` window hourly, so an offline machine backfills on
   reconnect.

Cost/accounting is entirely Langfuse-side — the shim only ships raw token
counts. See `docs/cost-and-tokens.md`. Note: re-importing cannot *remove* a tag
(Langfuse merges tags on re-ingest); changing the data model needs a deep wipe
of Langfuse first, not just a re-import.

## What lives here vs. what does NOT

- **HERE:** per-PC installers, the shim script, scheduled-task / systemd unit
  templates, README + this file.
- **NOT HERE:** the Langfuse cluster deployment (lives in `flux-home`),
  whisper-dictate-related work (lives in `whisper-dictate`).

When in doubt, ask before adding a new top-level directory.

## Conventions

- Shim language: Python 3.10+. Single file under `shim/`, no package layout
  until it earns it. Single runtime dep (`requests`) — keep it that way.
- Installers must be idempotent: re-running them on a machine that already has
  the shim should reconcile (update + re-register the schedule), not duplicate.
- No secrets in this repo. The shim reads its config from a `.env` file written
  by the installer; the user fills in the real values manually after install.
- Every script that runs unattended must log to a rolling local file
  (`~/.local/state/token-usage/ship.log` on Linux, `%LOCALAPPDATA%\token-usage\ship.log`
  on Windows) so failures are debuggable without re-running them interactively.

## Git safety

Same posture as `flux-home`: never push directly to `main`, always open a PR.
Bot/maintenance commits to `main` are allowed only for the initial scaffold
and version bumps; everything else goes through review.

## Testing

The shim has `pytest` coverage in `shim/test_ccusage_ship.py` (run with the
install's venv python, or any Python with `requests`), wired into CI via
`.github/workflows/test.yml`. It guards the Langfuse batch format —
`usageDetails`/no-`costDetails`, ids, native fields, and `_project_label`. The
installers have no automated tests yet; validate those manually on each target
OS before merging.

## Related repos

- `flux-home` — hosts the Langfuse deployment that this shim ships to.
- `whisper-dictate` — unrelated; the conversation that created this repo
  happened there by coincidence.
