# Setup & scheduling

Each machine runs `ccusage` locally and ships daily aggregates to your
Langfuse. Pick a package manager — each one installs the shim, pins `ccusage`,
and (except Nix) can set up an hourly importer. Then drop your Langfuse keys in
a `.env` and start the timer.

## Install

All channels pin the same upstream ccusage via `CCUSAGE_VERSION` at the repo
root (Renovate bumps it; merging the bump cuts the next release).

### Chocolatey (Windows)

```powershell
choco source add --name=token-usage `
  --source="https://factusconsulting.github.io/token-usage/chocolatey/index.json"
# omit -s so Chocolatey resolves the nodejs/python deps from the community feed
choco install token-usage -y
```

The Chocolatey package runs `installers/windows/install.ps1`, which also
registers a **Scheduled Task** (`TokenUsageCcusageShip`, hourly) and writes a
starter `.env` to `%LOCALAPPDATA%\token-usage\.env`. Upgrades (`choco upgrade`)
replace the shim in place — the task automatically runs the new version.

### Homebrew (macOS, Linux, WSL2)

```bash
brew tap FactusConsulting/tap
brew install token-usage
```

Then, for a scheduled importer (systemd timer on Linux, launchd on macOS) — no
repo clone, no hand-written unit:

```bash
mkdir -p ~/.config/token-usage
cp "$(brew --prefix token-usage)/libexec/shim/.env.example" ~/.config/token-usage/.env
"${EDITOR:-nano}" ~/.config/token-usage/.env      # fill in keys (see Configure)
brew services start token-usage                   # hourly timer
```

`brew upgrade token-usage` replaces the binary in place; the timer calls
`token-usage`, so the next run uses the new version automatically.

### Nix (flake)

```bash
nix run github:FactusConsulting/token-usage -- --help
```

The flake exposes `packages.default`/`packages.token-usage` (the wrapper),
`packages.token-usage-shim`, `apps.default`, and a `devShells.default`. Nix has
no scheduler — wire `token-usage` into your own systemd timer / launchd / cron.

### From a clone (no package manager)

`installers/windows/install.ps1` and `installers/ubuntu/install.sh` do the full
setup (Node + ccusage + shim + scheduled task / systemd timer) from a checkout.
The package managers are preferred; this is the fallback.

## Configure

The shim reads a `.env` from (in order) `$XDG_CONFIG_HOME/token-usage/.env`
(default `~/.config/token-usage/.env`), then next to the script, then the cwd.
Put config in the durable `~/.config` path so it survives `brew upgrade`.

```ini
# Required
LANGFUSE_HOST=https://langfuse.lwa.dk
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
CCUSAGE_SOURCES=claude,codex,openclaw  # trim to what this machine actually runs

# Optional
CCUSAGE_SINCE_DAYS=14                   # how many days back each run ships (default 14)
TOKEN_USAGE_HOSTNAME=my-desktop-wsl     # see "One machine, two installs" below
```

Verify before scheduling:

```bash
token-usage --dry-run                   # print the batch, don't send
```

## Running & backfilling

The timer ships a rolling `CCUSAGE_SINCE_DAYS` window each run (idempotent —
re-shipping a day upserts, never duplicates). A wider window backfills gaps if
the machine was offline; the default 14 covers a week or two of downtime.

For a one-off historical backfill:

```bash
token-usage --since-days 300            # last 300 days
token-usage --since 2026-01-01          # from an exact date
```

ccusage only has whatever history is in the machine's local logs, so a wide
window just ships everything available; days with no data ship nothing.

## One machine, two installs (Windows + WSL2)

Claude Code / Codex / pi etc. write their session logs to the home directory of
the OS they run in. A session in PowerShell writes to `C:\Users\<you>\...`; one
inside WSL2 writes to `/home/<you>/...`. ccusage on each side sees only its own
filesystem, so to capture both, install on **both**:

| Where you run AI tools | Install | Hostname |
| :--- | :--- | :--- |
| Windows / PowerShell | Chocolatey | Windows hostname (e.g. `LWA002`) |
| WSL2 | Homebrew inside WSL | set `TOKEN_USAGE_HOSTNAME=<host>-wsl2` |

Set a distinct `TOKEN_USAGE_HOSTNAME` on the WSL2 side — by default WSL shares
the Windows hostname, and Langfuse would merge the two sets of traces. The
hostname becomes the trace's host tag, so you can filter/group per machine in
the UI. The two sides are genuinely separate usage, not duplicates — confirm
WSL2's ccusage reads its own `~/.claude`, not the Windows logs via `/mnt/c`.

## Tuning `CCUSAGE_SOURCES`

Default is `claude,codex,openclaw,pi,copilot,gemini`. Trim per machine to the sources you
actually use — including ones that generate zero local data just adds empty
runs. See the [ccusage docs](https://ccusage.com/) for the full source list.
