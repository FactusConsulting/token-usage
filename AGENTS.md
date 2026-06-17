# Agent Operating Procedures — token-usage

Authoritative reference for AI agents working in this repository.

## Repository purpose

Cross-machine LLM token-usage tracking. Per-PC installers + a shim that ships
`ccusage` aggregates to a self-hosted Langfuse instance (deployed via
`flux-home/applications/langfuse/`). See README for architecture.

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

This repo has no test infrastructure yet (TODO). When adding the first test,
prefer `pytest` for the shim and PowerShell `Pester` / `bats` for the
installers. Until then, validate manually on each target OS before merging.

## Related repos

- `flux-home` — hosts the Langfuse deployment that this shim ships to.
- `whisper-dictate` — unrelated; the conversation that created this repo
  happened there by coincidence.
