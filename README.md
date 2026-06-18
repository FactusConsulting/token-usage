# token-usage

Cross-machine LLM token-usage tracking. Each machine runs [`ccusage`](https://ccusage.com/)
locally to read its own Claude Code / Codex / Copilot / Gemini session files,
then ships the daily aggregates to a self-hosted **Langfuse** so you see total
spend across every machine in one dashboard.

## Install

Each channel installs the shim and pins `ccusage`. Pick your platform:

**Windows (Chocolatey)** — also registers the hourly Scheduled Task:

```powershell
choco source add --name=token-usage `
  --source="https://factusconsulting.github.io/token-usage/chocolatey/index.json"
choco install token-usage -y
```

**macOS / Linux / WSL2 (Homebrew)**:

```bash
brew tap FactusConsulting/tap
brew install token-usage
brew services start token-usage     # hourly importer (systemd/launchd)
```

**Nix**: `nix run github:FactusConsulting/token-usage -- --help`

## Configure

Put your Langfuse keys in `~/.config/token-usage/.env` (a template ships with
the package):

```ini
LANGFUSE_HOST=https://langfuse.lwa.dk
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
CCUSAGE_SOURCES=claude,codex
```

Check it, then backfill history if you want:

```bash
token-usage --dry-run            # print the batch, don't send
token-usage --since-days 300     # one-off 300-day backfill
```

## Docs

- **[Setup & scheduling](docs/setup.md)** — per-platform install, the `.env`,
  the timer, backfills, and the Windows + WSL2 two-install pattern.
- **[Cost & tokens](docs/cost-and-tokens.md)** — how Langfuse computes cost,
  cache-token pricing, per-source model naming, idempotency.
- **[Design & rationale](docs/design.md)** — architecture, data model, why
  Langfuse, repo layout.

## License

MIT — see [LICENSE](LICENSE).
