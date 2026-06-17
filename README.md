# token-usage

Cross-machine LLM token-usage tracking. Each PC runs `ccusage` locally to read
its own Claude Code / Codex / Copilot / Gemini / etc. session files, then ships
the aggregated daily numbers to a self-hosted **Langfuse** instance so you can
see total spend across all your machines in one dashboard.

## Architecture

```
[PC A — Windows 11]                       [Home K8s cluster]
[PC B — Windows 11]   ccusage daily       ┌─────────────────────────┐
[PC C — Ubuntu]       --json --since=…    │  Langfuse               │
       │                                  │  (HelmRelease in        │
       │  HTTPS POST                      │   flux-home repo)       │
       │  /api/public/ingestion           │                         │
       └──────────►  langfuse.lwa.dk ────► │  Postgres + ClickHouse  │
                                          │  + MinIO + Redis        │
                                          └─────────────────────────┘
                                                      ▲
                                                      │
                                                  you, via UI
```

`ccusage` itself only produces aggregates (per-day / per-session / per-5h-block
totals — never per-request data). The shim wraps each daily row as one
**Langfuse trace** with **one generation** carrying the token counts; the host
name and source name go into `metadata` and `tags` so you can group by them
in the Langfuse UI.

## Repository layout

| Path | Purpose |
| :--- | :--- |
| `shim/ccusage-ship.py` | The actual shim. Reads `ccusage <source> daily --json` for every configured source and POSTs to Langfuse's ingestion API. Idempotent — re-running for the same day upserts. |
| `shim/requirements.txt` | Single dep: `requests`. (ccusage is invoked as a subprocess; not a Python lib.) |
| `shim/.env.example` | Template for the env file — Langfuse keys, hostname, source list. |
| `installers/windows/install.ps1` | Installs Node LTS + ccusage, drops the shim into `%LOCALAPPDATA%\token-usage\`, registers a Scheduled Task. |
| `installers/windows/scheduled-task.xml` | Template for the hourly run. |
| `installers/ubuntu/install.sh` | Installs Node 20 (nodesource), ccusage, the shim under `~/.local/share/token-usage/`, a systemd user timer. |
| `installers/ubuntu/ccusage-ship.{service,timer}` | systemd unit + timer running hourly. |

## Setup overview

1. **Deploy Langfuse** to your cluster via the manifests in `flux-home/applications/langfuse/` (see that PR). Once it is up at `https://langfuse.lwa.dk`, log in and create a project — note the public + secret API keys.
2. **Bootstrap each PC**:
   - Windows: `powershell -ExecutionPolicy Bypass -File installers/windows/install.ps1`
   - Ubuntu: `bash installers/ubuntu/install.sh`
3. On each PC, edit the generated `.env` and fill in `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST=https://langfuse.lwa.dk`, and the list of `CCUSAGE_SOURCES` you actually use.
4. The scheduled task / systemd timer runs `ccusage-ship.py` hourly. Verify in the Langfuse UI under that project's Traces.

## Why not LiteLLM or a proxy

Claude Code, Codex CLI, GitHub Copilot CLI and friends authenticate against
their vendor endpoints with OAuth/subscription tokens that won't transparently
re-route through a proxy. Tailing the local session files via `ccusage` is the
boring path that just works.

## Why not direct ccusage → Postgres → Grafana

You can — and at some point you probably should, because ccusage data is
aggregates, not traces, and Langfuse's trace model is mildly mismatched. The
reason we still start with Langfuse is that it gives one pane of glass for
LLM-related work (including future per-trace data from real apps) and the
extra fit cost is one half-empty observation per day per source.

## License

Private.
