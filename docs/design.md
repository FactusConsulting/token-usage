# Design & rationale

## Architecture

```
[PC A — Windows]                          [Home K8s cluster]
[PC B — WSL2]        ccusage daily        ┌─────────────────────────┐
[PC C — Ubuntu]      --json --since=…     │  Langfuse               │
       │                                  │  (HelmRelease in        │
       │  OTLP/HTTP JSON                  │   flux-home repo)       │
       │  /api/public/otel/v1/traces      │                         │
       └──────────►  langfuse.lwa.dk ────► │  Postgres + ClickHouse  │
                     (or langfuse.factus.dk) │  + MinIO + Redis      │
                                          └─────────────────────────┘
```

Each machine runs `ccusage` locally to read its own AI-tool session logs, asks
Langfuse (Observations API v2) what it already holds for the machine, and
sends the difference as OpenTelemetry spans. Nothing leaves the machine
except the daily token/cost aggregates.

## Data model

`ccusage` produces aggregates (per-day / per-session / per-5h totals — never
per-request data). By default the shim maps each ccusage **session** to:

- **one Langfuse trace** per session — id
  `ccusage-<host>-<source>-sess-<sessionId>`, mapped onto Langfuse's
  first-class fields: `userId` = host (Users view), `sessionId` = the ccusage
  session (Sessions view), `name` = `ccusage:<source>`. The only tag is the
  project (a machine-independent basename, so the same project unifies across
  machines); the full project path stays in metadata.
- **generations per model**, carrying token counts in `usageDetails`
  (input / output / cache-creation / cache-read). Each run adds one for
  whatever the session gained since the last.

Cost is left to Langfuse to compute from those token counts × its dated model
pricing. Langfuse v4 never updates a record, so nothing is re-sent: each run
ships only what Langfuse does not hold yet.
`CCUSAGE_GRANULARITY=daily` switches to one trace per calendar day instead. The
full details — cache-token pricing, per-source model naming, multi-machine
hostnames — are in [cost-and-tokens.md](cost-and-tokens.md).

## Why not LiteLLM or a proxy

Claude Code, Codex CLI, Copilot CLI and friends authenticate against their
vendor endpoints with OAuth/subscription tokens that won't transparently
re-route through a proxy. Tailing the local session files via `ccusage` is the
boring path that just works.

## Why not direct ccusage → Postgres → Grafana

You can — and at some point you probably should, because ccusage data is
aggregates, not traces, and Langfuse's trace model is a mild mismatch. We still
start with Langfuse because it gives one pane of glass for all LLM-related work
(including future per-trace data from real apps), and the extra fit cost is one
half-empty observation per day per source.

## Repository layout

| Path | Purpose |
| :--- | :--- |
| `shim/ccusage-ship.py` | The shim. Reads `ccusage <source> session --json` for each configured source and ships what Langfuse does not hold yet. |
| `shim/test_ccusage_ship.py` | Tests guarding the span attributes (usageDetails, model naming, ids, tags) and the difference against what Langfuse holds. |
| `shim/.env.example` | Config template. |
| `installers/windows/install.ps1` | Clone-based Windows install (also run by the Chocolatey package): shim + Scheduled Task. |
| `installers/ubuntu/install.sh` | Clone-based Linux/WSL2 install: shim + systemd user timer. |
| `packaging/{chocolatey,homebrew}/…` | Package definitions for the three channels. |

## Versioning & releases

`CCUSAGE_VERSION` at the repo root is the single source of truth for the pinned
upstream ccusage version. `ccusage.lock.json` next to it records, for each
platform, the URL and sha512 of the static binary upstream publishes as
`@ccusage/ccusage-<platform>` on npm. `scripts/ccusage_pin.py` writes both, and a
test fails if they disagree.

`.github/workflows/ccusage-update.yml` checks npm every hour. When ccusage has a
new version, it pins it, verifies every binary, runs the tests and the Nix build,
commits the pin to `main`, tags the next patch version and runs `release.yml`.
That builds the tarball, writes release notes with the upstream ccusage highlights,
publishes the Chocolatey feed and opens the Homebrew formula PR. Every ccusage
release therefore becomes a token-usage release within about an hour.

How each channel gets ccusage:

| Channel | ccusage |
| :--- | :--- |
| Nix flake | `packages.ccusage`: the pinned binary, fetched with its sha512 |
| Homebrew | `npm install` of `ccusage@<pin>` into the formula's private prefix |
| Chocolatey | the `ccusage` package on the same feed (pinned binary, sha512-checked), an exact-version dependency of `token-usage` |
| From a clone | `npm install -g ccusage@<pin>` by the installer scripts |

The Nix wrapper and the Chocolatey entry points (`token-usage.cmd`, the
Scheduled Task's `run-ship.pyw`) set `TOKEN_USAGE_CCUSAGE` to the binary's
absolute path, so the shim runs exactly the pinned ccusage even if another one
is on PATH. Without the
variable, the shim falls back to the first `ccusage` on PATH.

Other changes are still released by pushing a `v*` tag by hand.
