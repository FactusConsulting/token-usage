# Design & rationale

## Architecture

```
[PC A — Windows]                          [Home K8s cluster]
[PC B — WSL2]        ccusage daily        ┌─────────────────────────┐
[PC C — Ubuntu]      --json --since=…     │  Langfuse               │
       │                                  │  (HelmRelease in        │
       │  HTTPS POST                      │   flux-home repo)       │
       │  /api/public/ingestion           │                         │
       └──────────►  langfuse.lwa.dk ────► │  Postgres + ClickHouse  │
                     (or langfuse.factus.dk) │  + MinIO + Redis      │
                                          └─────────────────────────┘
```

Each machine runs `ccusage` locally to read its own AI-tool session logs and
POSTs the aggregated daily numbers to Langfuse. Nothing leaves the machine
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
- **one generation per model**, carrying token counts in Langfuse v3
  `usageDetails` (input / output / cache-creation / cache-read).

Cost is left to Langfuse to compute from those token counts × its dated model
pricing. Ids are deterministic, so re-running upserts instead of duplicating.
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
| `shim/ccusage-ship.py` | The shim. Reads `ccusage <source> daily --json` for each configured source and POSTs to Langfuse. Idempotent. |
| `shim/test_ccusage_ship.py` | Tests guarding the Langfuse batch format (usageDetails, model naming, ids, tags). |
| `shim/.env.example` | Config template. |
| `installers/windows/install.ps1` | Clone-based Windows install (also run by the Chocolatey package): shim + Scheduled Task. |
| `installers/ubuntu/install.sh` | Clone-based Linux/WSL2 install: shim + systemd user timer. |
| `packaging/{chocolatey,homebrew}/…` | Package definitions for the three channels. |

## Versioning & releases

`CCUSAGE_VERSION` at the repo root is the single source of truth for the pinned
upstream ccusage version. Renovate opens a PR when a new ccusage is published;
merging it triggers a release tag, which builds the tarball, publishes the
Chocolatey feed, and updates the Homebrew formula.
