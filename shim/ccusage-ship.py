"""Ship ccusage daily aggregates to a self-hosted Langfuse instance.

Reads `ccusage <source> daily --json --since=<since>` for each configured
source, then upserts one Langfuse trace per (host, source, date) and one
generation observation per model used that day. Re-runs are idempotent
because Langfuse's ingestion API treats `id` as the stable upsert key.

Configuration comes from environment variables — the installer writes a `.env`
file that this script picks up via `python-dotenv` if installed, or you can
just `export` them in the shell that runs the cron / systemd unit.

Env vars:
    LANGFUSE_HOST          e.g. https://langfuse.lwa.dk  (required)
    LANGFUSE_PUBLIC_KEY    pk-lf-...                     (required)
    LANGFUSE_SECRET_KEY    sk-lf-...                     (required)
    CCUSAGE_SOURCES        comma-separated list, e.g.
                           "claude,codex,copilot,gemini" (required)
    CCUSAGE_SINCE_DAYS     how many days of history to (re-)ship per run.
                           Default 7 — covers DST / cron skips without
                           ballooning request volume.
    TOKEN_USAGE_HOSTNAME   override the hostname used as the trace
                           grouping key (default: socket.gethostname()).
    TOKEN_USAGE_DRY_RUN    if set to "1", print what would be POSTed
                           instead of sending. Useful for first-time
                           validation.

Exit codes:
    0  success (all source/day combinations ingested or already up to date)
    1  configuration error (missing env vars, etc.)
    2  one or more sources failed; check the log
"""
from __future__ import annotations

import functools
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

REQUIRED_ENV = ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
                "CCUSAGE_SOURCES")


def _load_dotenv_best_effort() -> None:
    """Load a `.env` next to this script if python-dotenv is available.

    We don't make python-dotenv a hard dep — the systemd/scheduled-task units
    can also export the vars directly. Failing softly here keeps the shim a
    single-file install."""
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except ImportError:
        return
    candidates = [
        Path(__file__).with_name(".env"),
        Path.cwd() / ".env",
    ]
    for path in candidates:
        if path.is_file():
            load_dotenv(path)
            return


def _require_env() -> tuple[str, str, str, list[str]]:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        sys.stderr.write(
            f"[ccusage-ship] missing required env vars: {', '.join(missing)}\n")
        sys.exit(1)
    host = os.environ["LANGFUSE_HOST"].rstrip("/")
    pk = os.environ["LANGFUSE_PUBLIC_KEY"]
    sk = os.environ["LANGFUSE_SECRET_KEY"]
    sources = [s.strip() for s in os.environ["CCUSAGE_SOURCES"].split(",")
               if s.strip()]
    if not sources:
        sys.stderr.write("[ccusage-ship] CCUSAGE_SOURCES is empty\n")
        sys.exit(1)
    return host, pk, sk, sources


@functools.lru_cache(maxsize=1)
def _ccusage_exe() -> str:
    """Resolve the ccusage executable. On Windows `npm -g` installs
    `ccusage.cmd`, which subprocess can't launch by the bare name `ccusage`
    (CreateProcess does no PATHEXT search), so resolve the full path via
    shutil.which. Raises a clear error if ccusage isn't on PATH at all."""
    exe = shutil.which("ccusage")
    if not exe:
        raise FileNotFoundError(
            "ccusage not found on PATH. Install it with "
            "`npm install -g ccusage` (the installers do this for you)."
        )
    return exe


def _ccusage_daily(source: str, since: str) -> list[dict[str, Any]]:
    """Run `ccusage <source> daily --json --since <since>` and return the
    `daily` array. Raises CalledProcessError on a non-zero exit so the caller
    can decide whether to fail the whole run or just skip this source."""
    out = subprocess.check_output(
        [_ccusage_exe(), source, "daily", "--json", "--since", since],
        text=True,
    )
    payload = json.loads(out)
    return list(payload.get("daily") or [])


def _build_batch(rows: list[dict[str, Any]], host: str,
                 source: str) -> list[dict[str, Any]]:
    """Map ccusage daily rows to a Langfuse ingestion batch.

    Each daily row becomes:
      * one trace-create event (id = ccusage-<host>-<source>-<date>)
      * one generation-create event per modelsUsed entry, with the per-model
        usage attached as Langfuse's standard `usage` block

    ccusage reports per-model breakdowns when `modelBreakdowns` is present
    (newer versions), but the top-level totals are always there. If only the
    totals are available we attribute everything to a single synthetic
    "aggregate" model so the dashboard still adds up.
    """
    events: list[dict[str, Any]] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for row in rows:
        date = row.get("date")
        if not date:
            continue
        trace_id = f"ccusage-{host}-{source}-{date}"
        events.append({
            "id": f"{trace_id}-trace-{int(time.time() * 1000)}",
            "type": "trace-create",
            "timestamp": now_iso,
            "body": {
                "id": trace_id,
                "name": f"ccusage:{source}",
                "timestamp": f"{date}T00:00:00Z",
                "metadata": {
                    "host": host,
                    "source": source,
                    "date": date,
                    "cache_creation_tokens": row.get("cacheCreationTokens", 0),
                    "cache_read_tokens": row.get("cacheReadTokens", 0),
                },
                "tags": [host, source, f"date:{date}"],
            },
        })

        breakdowns = row.get("modelBreakdowns") or []
        if not breakdowns:
            breakdowns = [{
                "modelName": (row.get("modelsUsed") or ["aggregate"])[0],
                "inputTokens": row.get("inputTokens", 0),
                "outputTokens": row.get("outputTokens", 0),
                "cacheCreationTokens": row.get("cacheCreationTokens", 0),
                "cacheReadTokens": row.get("cacheReadTokens", 0),
                "cost": row.get("totalCost", 0.0),
            }]

        for bd in breakdowns:
            model = bd.get("modelName") or "unknown"
            gen_id = f"{trace_id}-gen-{model}"
            events.append({
                "id": f"{gen_id}-evt-{int(time.time() * 1000)}",
                "type": "generation-create",
                "timestamp": now_iso,
                "body": {
                    "id": gen_id,
                    "traceId": trace_id,
                    "name": f"{source}:{model}",
                    "model": model,
                    "startTime": f"{date}T00:00:00Z",
                    "endTime": f"{date}T23:59:59Z",
                    "usage": {
                        "input": int(bd.get("inputTokens", 0)),
                        "output": int(bd.get("outputTokens", 0)),
                        "total": int(bd.get("inputTokens", 0))
                                 + int(bd.get("outputTokens", 0)),
                        "unit": "TOKENS",
                        "inputCost": 0,
                        "outputCost": 0,
                        "totalCost": float(bd.get("cost", 0.0)),
                    },
                    "metadata": {
                        "cache_creation_tokens": int(
                            bd.get("cacheCreationTokens", 0)),
                        "cache_read_tokens": int(
                            bd.get("cacheReadTokens", 0)),
                    },
                },
            })
    return events


def _ship(host_url: str, pk: str, sk: str,
          batch: list[dict[str, Any]]) -> None:
    if not batch:
        return
    if os.environ.get("TOKEN_USAGE_DRY_RUN") == "1":
        print(json.dumps({"batch": batch}, indent=2))
        return
    resp = requests.post(
        f"{host_url}/api/public/ingestion",
        auth=(pk, sk),
        json={"batch": batch},
        timeout=30,
    )
    if resp.status_code >= 400:
        sys.stderr.write(
            f"[ccusage-ship] Langfuse rejected batch: "
            f"HTTP {resp.status_code} {resp.text[:500]}\n")
        resp.raise_for_status()


def main() -> int:
    _load_dotenv_best_effort()
    langfuse_host, pk, sk, sources = _require_env()
    host = os.environ.get("TOKEN_USAGE_HOSTNAME") or socket.gethostname()
    try:
        since_days = int(os.environ.get("CCUSAGE_SINCE_DAYS", "7"))
    except ValueError:
        since_days = 7
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)
             ).date().isoformat()

    failures: list[str] = []
    for source in sources:
        try:
            rows = _ccusage_daily(source, since)
        except subprocess.CalledProcessError as exc:
            sys.stderr.write(
                f"[ccusage-ship] {source}: ccusage exited "
                f"{exc.returncode}\n")
            failures.append(source)
            continue
        except (json.JSONDecodeError, FileNotFoundError) as exc:
            sys.stderr.write(f"[ccusage-ship] {source}: {exc}\n")
            failures.append(source)
            continue
        batch = _build_batch(rows, host, source)
        try:
            _ship(langfuse_host, pk, sk, batch)
        except requests.RequestException as exc:
            sys.stderr.write(f"[ccusage-ship] {source}: ship failed: {exc}\n")
            failures.append(source)
            continue
        sys.stderr.write(
            f"[ccusage-ship] {source}: shipped {len(rows)} day(s), "
            f"{len(batch)} event(s)\n")

    return 2 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
