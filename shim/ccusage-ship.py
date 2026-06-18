"""Ship ccusage usage to a self-hosted Langfuse instance.

Reads `ccusage <source> <granularity> --json --since=<since>` for each
configured source, then upserts one Langfuse trace per session (default) — or
per day — and one generation observation per model. Re-runs are idempotent
because Langfuse's ingestion API treats `id` as the stable upsert key.

Configuration comes from environment variables. They can be `export`ed by the
shell/cron/systemd unit, or written to a `.env` that this script picks up via
`python-dotenv` (if installed). The `.env` is searched for in this order:
    1. $XDG_CONFIG_HOME/token-usage/.env  (default ~/.config/token-usage/.env)
    2. next to this script
    3. the current working directory
The config-dir copy (1) is the durable one: it works from any cwd and survives
a `brew upgrade`, which wipes the copy shipped next to the script.

CLI flags (all optional; without them the env-var defaults apply):
    --since-days N        (re-)ship the last N days; overrides CCUSAGE_SINCE_DAYS
    --since YYYY-MM-DD     (re-)ship since an exact date
    --granularity G       "session" (default) or "daily"; overrides
                          CCUSAGE_GRANULARITY
    --dry-run             print the batch instead of POSTing
e.g. a one-off backfill from anywhere:  token-usage --since-days 300

Env vars:
    LANGFUSE_HOST          e.g. https://langfuse.lwa.dk  (required)
    LANGFUSE_PUBLIC_KEY    pk-lf-...                     (required)
    LANGFUSE_SECRET_KEY    sk-lf-...                     (required)
    CCUSAGE_SOURCES        comma-separated list, e.g.
                           "claude,codex,copilot,gemini" (required)
    CCUSAGE_SINCE_DAYS     how many days of history to (re-)ship per run.
                           Default 14 — wide enough that a machine offline for
                           a week or two backfills the gap on its next run,
                           without ballooning request volume (upserts dedupe).
    CCUSAGE_GRANULARITY    "session" (default) or "daily". Session groups one
                           trace per ccusage session (tagged with its project);
                           daily groups one trace per calendar day.
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

import argparse
import functools
import json
import os
import re
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


def _config_dir() -> Path:
    """Durable per-user config dir for token-usage (XDG-aware)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "token-usage"


def _load_dotenv_best_effort() -> None:
    """Load the first `.env` found, searching (in order) the durable per-user
    config dir, then next to this script, then the current directory.

    The config-dir lookup is what lets a manual `token-usage` run work from any
    cwd and survive a `brew upgrade`, which wipes the copy shipped next to the
    script under the Cellar. We don't make python-dotenv a hard dep — the
    systemd/scheduled-task units can also export the vars directly — so failing
    softly here keeps the shim a single-file install."""
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except ImportError:
        return
    candidates = [
        _config_dir() / ".env",
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


def _ccusage_rows(source: str, since: str,
                  granularity: str) -> list[dict[str, Any]]:
    """Run `ccusage <source> <granularity> --json --since <since>` and return
    the rows. granularity is "session" (default) or "daily". Raises
    CalledProcessError on a non-zero exit so the caller can decide whether to
    fail the whole run or just skip this source."""
    out = subprocess.check_output(
        [_ccusage_exe(), source, granularity, "--json", "--since", since],
        text=True,
    )
    payload = json.loads(out)
    key = "sessions" if granularity == "session" else "daily"
    return list(payload.get(key) or [])


def _project_label(project_path: str | None) -> str | None:
    """Turn ccusage's encoded project path into a short, machine-independent
    tag. ccusage reports the path the way Claude Code encodes it on disk
    (separators collapsed to `-`), e.g. "D--source-whisper-dictate" =
    D:\\source\\whisper-dictate and "-home-lars-source-homelab" =
    /home/lars/source/homelab. We keep the segment after a `source` parent —
    so the same project under ~/source unifies across Windows and WSL2
    (`whisper-dictate`, `homelab`, ...) — and fall back to the raw path for
    anything not under a `source` dir. The full path stays in metadata.
    """
    if not project_path:
        return None
    # Drop per-run scaffolding ccusage appends for subagent sessions
    # (".../<uuid>/subagents/workflows") — the project is the first path
    # segment; the rest just fragments the label.
    head = project_path.split("/", 1)[0]
    m = re.search(r"source[-/]([^/]+)", head)
    # Strip leading/trailing separators the path encoding leaves behind (a
    # trailing slash becomes a trailing `-`, so e.g. ".../voice-pi/" ->
    # "voice-pi--"); internal hyphens in the name are kept.
    label = (m.group(1) if m else head).strip("-")
    return label or None


def _build_batch(rows: list[dict[str, Any]], host: str, source: str,
                 granularity: str = "session") -> list[dict[str, Any]]:
    """Map ccusage rows to a Langfuse ingestion batch.

    granularity is "session" (default) or "daily":
      * session — one trace per ccusage sessionId, timestamped at the session's
        first activity and tagged with its project, so you can see which
        session / project drove the spend.
      * daily — one trace per calendar day.

    Either way each trace gets one generation-create event per model, carrying
    token counts in Langfuse v3 `usageDetails` (incl. cache tokens); cost is
    left to Langfuse to compute from its dated model pricing.

    ccusage reports per-model breakdowns when `modelBreakdowns` is present
    (claude). When it isn't, the model name still lives elsewhere: codex puts
    it in a `models` dict (e.g. {"gpt-5.5": {...}}). We use that real model
    name so Langfuse can price the generation; only if no model name is found
    anywhere do we fall back to the source name.
    """
    events: list[dict[str, Any]] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for row in rows:
        session_id: str | None = None
        if granularity == "daily":
            date = row.get("date")
            if not date:
                continue
            trace_id = f"ccusage-{host}-{source}-{date}"
            trace_ts = start = f"{date}T00:00:00Z"
            end = f"{date}T23:59:59Z"
            metadata = {"host": host, "source": source, "date": date}
            tags: list[str] = []
        else:  # session
            sid = row.get("sessionId")
            if not sid:
                continue
            session_id = sid
            trace_id = f"ccusage-{host}-{source}-sess-{sid}"
            start = row.get("firstActivity") or now_iso
            end = row.get("lastActivity") or start
            trace_ts = start
            project = row.get("projectPath")
            label = _project_label(project)
            metadata = {"host": host, "source": source, "sessionId": sid,
                        "project": project,
                        "firstActivity": start, "lastActivity": end}
            # host -> userId, source -> trace name, so the ONLY tag is the
            # project (a machine-independent label). Keeps the tag picker clean.
            tags = [f"project:{label}"] if label else []

        # Map our dimensions onto Langfuse's first-class fields: host -> userId
        # (Users view), source -> name, session -> sessionId (Sessions view).
        trace_body: dict[str, Any] = {
            "id": trace_id,
            "name": f"ccusage:{source}",
            "timestamp": trace_ts,
            "userId": host,
            "metadata": metadata,
            "tags": tags,
        }
        if session_id:
            trace_body["sessionId"] = session_id
        events.append({
            "id": f"{trace_id}-trace-{int(time.time() * 1000)}",
            "type": "trace-create",
            "timestamp": now_iso,
            "body": trace_body,
        })

        breakdowns = row.get("modelBreakdowns") or []
        if not breakdowns:
            # Sources without modelBreakdowns (e.g. codex) still name their
            # model elsewhere: claude uses `modelsUsed` (a list), codex uses
            # `models` (a dict keyed by model name, e.g. {"gpt-5.5": {...}}).
            # Prefer the real model name so Langfuse can price it (a generic
            # "codex" label matches no model definition -> cost 0); fall back
            # to the source name only if neither is present.
            model_name = (
                row.get("modelsUsed")
                or list(row.get("models") or {})
                or [source]
            )[0]
            breakdowns = [{
                "modelName": model_name,
                "inputTokens": row.get("inputTokens", 0),
                "outputTokens": row.get("outputTokens", 0),
                "cacheCreationTokens": row.get("cacheCreationTokens", 0),
                "cacheReadTokens": row.get("cacheReadTokens", 0),
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
                    "startTime": start,
                    "endTime": end,
                    # usageDetails (Langfuse v3) makes every token type
                    # first-class: cache tokens count toward the totals and
                    # show up in "Usage by type", instead of being buried in
                    # metadata where Langfuse ignores them. Langfuse sums these
                    # for the trace's total tokens. For Claude the cache_read
                    # bucket dwarfs input/output, so this is the difference
                    # between the dashboard undercounting by ~100x and being
                    # accurate. Key names match Anthropic's so Langfuse model
                    # pricing can map cache rates when defined.
                    "usageDetails": {
                        "input": int(bd.get("inputTokens", 0)),
                        "output": int(bd.get("outputTokens", 0)),
                        "cache_creation_input_tokens": int(
                            bd.get("cacheCreationTokens", 0)),
                        "cache_read_input_tokens": int(
                            bd.get("cacheReadTokens", 0)),
                    },
                    # No costDetails: Langfuse computes cost itself from these
                    # token counts x its dated model prices (historically
                    # correct), and leaves self-hosted models with no price
                    # definition at 0 instead of a bogus number.
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
    # Post in chunks so a source with many sessions stays well under Langfuse's
    # request-size limit; each chunk is an independent ingestion request, and
    # the deterministic ids mean a retried/overlapping chunk just upserts.
    for i in range(0, len(batch), 100):
        chunk = batch[i:i + 100]
        # Retry transient network failures (the in-cluster gateway occasionally
        # resets the connection mid-request); deterministic ids make a retried
        # chunk a safe upsert. Give up after a few tries and let the caller mark
        # the source failed — the next scheduled run backfills it.
        resp = None
        for attempt in range(4):
            try:
                resp = requests.post(
                    f"{host_url}/api/public/ingestion",
                    auth=(pk, sk),
                    json={"batch": chunk},
                    timeout=30,
                )
                break
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))
        if resp.status_code >= 400:
            sys.stderr.write(
                f"[ccusage-ship] Langfuse rejected batch: "
                f"HTTP {resp.status_code} {resp.text[:500]}\n")
            resp.raise_for_status()
        # 207 multi-status: the request succeeded but some events may have been
        # rejected. Surface that instead of silently undercounting.
        try:
            errors = (resp.json() or {}).get("errors") or []
        except ValueError:
            errors = []
        if errors:
            sys.stderr.write(
                f"[ccusage-ship] {len(errors)} event(s) rejected by Langfuse: "
                f"{json.dumps(errors[:2])[:400]}\n")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="token-usage",
        description="Ship ccusage usage (per session by default) to a "
                    "self-hosted Langfuse instance.")
    window = p.add_mutually_exclusive_group()
    window.add_argument(
        "--since-days", type=int, metavar="N", default=None,
        help="(Re-)ship the last N days. Overrides CCUSAGE_SINCE_DAYS. "
             "Handy for one-off backfills, e.g. --since-days 300.")
    window.add_argument(
        "--since", metavar="YYYY-MM-DD", default=None,
        help="(Re-)ship since this exact date. Overrides --since-days and "
             "CCUSAGE_SINCE_DAYS.")
    p.add_argument(
        "--granularity", choices=["session", "daily"], default=None,
        help="Group traces by session (default) or by day. Overrides "
             "CCUSAGE_GRANULARITY. Session shows which session/project drove "
             "the spend; daily is one trace per calendar day.")
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print the batch instead of POSTing (same as "
             "TOKEN_USAGE_DRY_RUN=1).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    _load_dotenv_best_effort()
    langfuse_host, pk, sk, sources = _require_env()
    if args.dry_run:
        os.environ["TOKEN_USAGE_DRY_RUN"] = "1"
    host = os.environ.get("TOKEN_USAGE_HOSTNAME") or socket.gethostname()
    granularity = (args.granularity
                   or os.environ.get("CCUSAGE_GRANULARITY") or "session")
    if granularity not in ("session", "daily"):
        granularity = "session"
    if args.since:
        since = args.since
    else:
        if args.since_days is not None:
            since_days = args.since_days
        else:
            try:
                since_days = int(os.environ.get("CCUSAGE_SINCE_DAYS", "14"))
            except ValueError:
                since_days = 14
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)
                 ).date().isoformat()

    failures: list[str] = []
    for source in sources:
        try:
            rows = _ccusage_rows(source, since, granularity)
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
        batch = _build_batch(rows, host, source, granularity)
        try:
            _ship(langfuse_host, pk, sk, batch)
        except requests.RequestException as exc:
            sys.stderr.write(f"[ccusage-ship] {source}: ship failed: {exc}\n")
            failures.append(source)
            continue
        unit = "session" if granularity == "session" else "day"
        sys.stderr.write(
            f"[ccusage-ship] {source}: shipped {len(rows)} {unit}(s), "
            f"{len(batch)} event(s)\n")

    return 2 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
