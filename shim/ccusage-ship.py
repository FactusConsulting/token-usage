"""Ship ccusage usage to a self-hosted Langfuse v4 instance.

Reads `ccusage <source> <granularity> --json --since=<since>` for each
configured source and sends one Langfuse trace per session (default) — or per
day — over OpenTelemetry (OTLP/HTTP JSON, `x-langfuse-ingestion-version: 4`).

Langfuse v4 stores observations append-only: a record re-sent with the same id
is NOT an update, it is a second row, and every sum counts both. So nothing is
ever re-sent. Each run asks Langfuse what it already holds for this host
(Observations API v2), compares it with what ccusage reports now, and ships
only the difference — per session (or day) and model — as a new generation.
A session that keeps growing gets one more generation per run in the same
trace; the totals add up to ccusage's. Langfuse is the only state, so a wider
window (`--since-days 300`) backfills what is missing without duplicating, on
any machine, after any reinstall.

`--reship` is for correcting data already in Langfuse (a changed usage key, a
price that needs the token counts re-sent): it deletes the traces of every
session in the window that ccusage still has, waits until Langfuse confirms
they are gone, and then ships those sessions in full. Sessions ccusage no
longer has on disk are never deleted, so history older than the local logs
survives.

Configuration comes from environment variables. They can be `export`ed by the
shell/cron/systemd unit, or written to a `.env` that this script picks up via
`python-dotenv` (if installed). The `.env` is searched for in this order:
    1. $XDG_CONFIG_HOME/token-usage/.env  (default ~/.config/token-usage/.env)
    2. next to this script
    3. the current working directory
The config-dir copy (1) is the durable one: it works from any cwd and survives
a `brew upgrade`, which wipes the copy shipped next to the script.

CLI flags (all optional; without them the env-var defaults apply):
    --since-days N        ship what is missing from the last N days;
                          overrides CCUSAGE_SINCE_DAYS
    --since YYYY-MM-DD     ship what is missing since an exact date
    --granularity G       "session" (default) or "daily"; overrides
                          CCUSAGE_GRANULARITY
    --reship              delete and re-send the window's sessions in full
    --dry-run             print what would be sent/deleted, change nothing
    --version             print the token-usage and ccusage versions
    --self-test           send a canary trace, read it back, check every
                          field mapped, delete it (run after a Langfuse
                          upgrade: the OTel attribute names are Langfuse's)
e.g. a one-off backfill from anywhere:  token-usage --since-days 300

Env vars:
    LANGFUSE_HOST          e.g. https://langfuse.lwa.dk  (required)
    LANGFUSE_PUBLIC_KEY    pk-lf-...                     (required)
    LANGFUSE_SECRET_KEY    sk-lf-...                     (required)
    CCUSAGE_SOURCES        comma-separated list, e.g.
                           "claude,codex,openclaw,copilot,gemini" (required)
    CCUSAGE_SINCE_DAYS     how many days back each run compares. Default 14 —
                           wide enough that a machine offline for a week or
                           two backfills the gap on its next run.
    CCUSAGE_GRANULARITY    "session" (default) or "daily". Session groups one
                           trace per ccusage session (tagged with its project);
                           daily groups one trace per calendar day.
    TOKEN_USAGE_HOSTNAME   override the hostname used as the trace
                           grouping key (default: socket.gethostname()).
    HEARTBEAT_URL          optional dead-man's-switch monitor URL (Uptime
                           Kuma push, healthchecks.io, …), pinged with a GET
                           on a fully successful run only. A missing ping —
                           timer never fired, ccusage broke, Langfuse
                           unreachable — trips the monitor's alert. Use one
                           URL per machine; the ping never fails the run.
    TOKEN_USAGE_DRY_RUN    if set to "1", same as --dry-run.

Exit codes:
    0  success (every source is up to date in Langfuse)
    1  configuration error (missing env vars, another run holds the lock)
    2  one or more sources failed; check the log
"""
from __future__ import annotations

import argparse
import functools
import hashlib
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
from typing import Any, Iterator

import requests

# Stamped with the release tag by .github/workflows/release.yml; a checkout says
# "dev".
__version__ = "dev"

REQUIRED_ENV = ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
                "CCUSAGE_SOURCES")

OTLP_PATH = "/api/public/otel/v1/traces"
OBSERVATIONS_PATH = "/api/public/v2/observations"
TRACES_PATH = "/api/public/traces"
# Without this header Langfuse v4 routes OTel spans through its staging
# pipeline, which delays them by up to 15 minutes. With it they are visible in
# seconds — which the next run's comparison depends on.
INGESTION_HEADERS = {"x-langfuse-ingestion-version": "4"}

# The four token counts compared and shipped. ccusage's names on the left.
TOKEN_FIELDS = ("inputTokens", "outputTokens", "cacheCreationTokens",
                "cacheReadTokens")

# How long --reship waits for Langfuse's asynchronous trace deletion before it
# gives up on a source (shipping into a trace still being deleted would let
# the deletion take the new data with it).
DELETE_TIMEOUT_S = 600


def _config_dir() -> Path:
    """Durable per-user config dir for token-usage (XDG-aware)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "token-usage"


def _state_dir() -> Path:
    """Per-user state dir: where the run lock lives."""
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "token-usage"
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
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


# ccusage is a Node script. When the shim is launched from a GUI-subsystem
# host (pythonw.exe, used by the Windows Scheduled Task so nothing flashes on
# screen), spawning a console child would pop a console window every run.
# CREATE_NO_WINDOW suppresses it. The flag is Windows-only; 0 is a no-op
# default everywhere else, so the call site can pass it unconditionally.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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
        creationflags=_NO_WINDOW,
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


def _cache_read_key(model: str) -> str:
    """The usageDetails key Langfuse prices cache reads under, per provider.

    Langfuse's built-in price table is provider-specific about this, and a key
    it does not price is silently worth nothing — the tokens still show up in
    the dashboard, but they contribute $0 to cost and nothing errors:

      * Anthropic entries price both `cache_read_input_tokens` (Anthropic's own
        wire name) and `input_cache_read`.
      * OpenAI entries price only `input_cache_read` / `input_cached_tokens`.

    Emit exactly one key, chosen by model family, so the totals stay correct
    (Langfuse sums usageDetails).
    """
    head = model.split("/")[-1].lower()
    if head.startswith("claude") or head.startswith("anthropic."):
        return "cache_read_input_tokens"
    return "input_cache_read"


def _usage_details(model: str, bd: dict[str, Any]) -> dict[str, int]:
    """Token counts for one model's generation, keyed the way Langfuse's price
    table for that provider expects. Zero counts are left out: Langfuse treats
    a missing key as zero, and a delta generation usually moves one or two."""
    details = {
        "input": int(bd.get("inputTokens", 0)),
        "output": int(bd.get("outputTokens", 0)),
        # Anthropic charges a premium for cache writes; OpenAI does not price
        # them at all, and ccusage reports 0 there.
        "cache_creation_input_tokens": int(bd.get("cacheCreationTokens", 0)),
        _cache_read_key(model): int(bd.get("cacheReadTokens", 0)),
    }
    return {k: v for k, v in details.items() if v}


def _tokens_from_usage_details(details: dict[str, Any]) -> dict[str, int]:
    """Inverse of _usage_details, for what Langfuse already holds. Both
    cache-read keys count: data shipped before the per-provider key was
    introduced carries Anthropic's name for every model."""
    def n(key: str) -> int:
        try:
            return int(details.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    return {
        "inputTokens": n("input"),
        "outputTokens": n("output"),
        "cacheCreationTokens": n("cache_creation_input_tokens"),
        "cacheReadTokens": n("cache_read_input_tokens") + n("input_cache_read"),
    }


# --- ccusage rows -> units ---------------------------------------------------

_ROLLOUT_TS = re.compile(r"rollout-(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})")


def _session_start(row: dict[str, Any]) -> str | None:
    """A session's first activity. codex reports none; its session id embeds
    the rollout file's creation time ("…/rollout-2026-05-28T19-05-55-…"),
    which is when the session began."""
    if row.get("firstActivity"):
        return row["firstActivity"]
    m = _ROLLOUT_TS.search(str(row.get("sessionId") or row.get("sessionFile")
                               or ""))
    if m:
        return f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}Z"
    return row.get("lastActivity")


def _model_breakdowns(row: dict[str, Any], source: str) -> list[dict[str, Any]]:
    """Per-model token counts for one row.

    ccusage reports per-model breakdowns when `modelBreakdowns` is present
    (claude, pi). When it isn't, the model name still lives elsewhere: claude
    uses `modelsUsed` (a list), codex a `models` dict keyed by model name
    (e.g. {"gpt-5.5": {...}}). The real name is what lets Langfuse price the
    generation; the source name is only the last resort."""
    breakdowns = row.get("modelBreakdowns")
    if breakdowns:
        return breakdowns
    models = row.get("models")
    if isinstance(models, dict) and models:
        return [{"modelName": name, **(counts or {})}
                for name, counts in models.items()]
    model_name = (row.get("modelsUsed") or [source])[0]
    return [{"modelName": model_name,
             **{f: row.get(f, 0) for f in TOKEN_FIELDS}}]


def _units(rows: list[dict[str, Any]], host: str, source: str,
           granularity: str) -> list[dict[str, Any]]:
    """Normalize ccusage rows into units: one trace each (a session or a day),
    with the cumulative token counts per model ccusage reports for it now."""
    units: list[dict[str, Any]] = []
    for row in rows:
        if granularity == "daily":
            date = row.get("date")
            if not date:
                continue
            key = ("daily", date)
            trace_key = f"ccusage-{host}-{source}-{date}"
            start, end = f"{date}T00:00:00Z", f"{date}T23:59:59Z"
            metadata = {"host": host, "source": source, "date": date}
            session_id, tags = None, []
        else:
            sid = row.get("sessionId")
            start = _session_start(row)
            if not sid or not start:
                continue
            key = ("session", sid)
            trace_key = f"ccusage-{host}-{source}-sess-{sid}"
            end = row.get("lastActivity") or start
            project = row.get("projectPath")
            label = _project_label(project)
            metadata = {"host": host, "source": source, "sessionId": sid}
            if project:
                metadata["project"] = project
            session_id = sid
            # host -> userId, source -> trace name, so the ONLY tag is the
            # project (a machine-independent label).
            tags = [f"project:{label}"] if label else []
        models: dict[str, dict[str, int]] = {}
        for bd in _model_breakdowns(row, source):
            model = bd.get("modelName") or "unknown"
            counts = models.setdefault(model, dict.fromkeys(TOKEN_FIELDS, 0))
            for f in TOKEN_FIELDS:
                counts[f] += int(bd.get(f) or 0)
        units.append({
            "source": source, "key": key, "trace_key": trace_key,
            "trace_id": _hex_id(trace_key, 32),
            "start": start, "end": end, "session_id": session_id,
            "tags": tags, "metadata": metadata, "models": models,
        })
    return units


# --- what Langfuse already holds --------------------------------------------

def _unit_key_of(obs: dict[str, Any]) -> tuple[str, str] | None:
    md = obs.get("metadata") or {}
    if md.get("sessionId") or obs.get("sessionId"):
        return ("session", md.get("sessionId") or obs["sessionId"])
    if md.get("date"):
        return ("daily", md["date"])
    return None


def _source_of(obs: dict[str, Any]) -> str | None:
    md = obs.get("metadata") or {}
    if md.get("source"):
        return md["source"]
    name = obs.get("traceName") or ""
    return name.split(":", 1)[1] if name.startswith("ccusage:") else None


def _iter_observations(host_url: str, auth: tuple[str, str], host: str,
                       from_start: str) -> Iterator[dict[str, Any]]:
    """Every generation Langfuse holds for this host from `from_start` on,
    following the v2 cursor."""
    params: dict[str, Any] = {
        "userId": host, "type": "GENERATION", "fromStartTime": from_start,
        "fields": "core,basic,usage,model,metadata,trace_context",
        "limit": 1000,
    }
    while True:
        resp = _request("GET", f"{host_url}{OBSERVATIONS_PATH}", auth,
                        params=params)
        payload = resp.json()
        yield from payload.get("data") or []
        cursor = (payload.get("meta") or {}).get("cursor")
        if not cursor:
            return
        params["cursor"] = cursor


def _shipped(observations: Iterator[dict[str, Any]]) -> dict[str, Any]:
    """Fold Langfuse's generations into what has been shipped per
    (source, unit, model): token totals, the latest end time, and the trace ids
    holding them.

    Rows sharing an id are one observation, counted once — the data the
    pre-v4 shim re-sent hourly under the same id shows up that way until it is
    cleaned up, and so does a POST retried after a timeout. The newest copy
    (the largest totals; ccusage counts only grow) wins."""
    by_id: dict[str, dict[str, Any]] = {}
    for obs in observations:
        oid = obs.get("id")
        # Legacy traces are not counted: Langfuse's dual write copies them
        # into v4 incompletely, and _migrate_legacy moves them over whole.
        if not oid or _is_legacy(obs.get("traceId")):
            continue
        prev = by_id.get(oid)
        if prev is None or (_total(obs) >= _total(prev)):
            by_id[oid] = obs
    shipped: dict[tuple[str, tuple[str, str], str], dict[str, Any]] = {}
    for obs in by_id.values():
        source, key = _source_of(obs), _unit_key_of(obs)
        if not source or not key:
            continue
        entry = shipped.setdefault(
            (source, key, obs.get("model") or "unknown"),
            {"tokens": dict.fromkeys(TOKEN_FIELDS, 0), "end": None,
             "trace_ids": set()})
        for f, v in _tokens_from_usage_details(
                obs.get("usageDetails") or {}).items():
            entry["tokens"][f] += v
        end = obs.get("endTime") or obs.get("startTime")
        if end and (entry["end"] is None or _ts(end) > _ts(entry["end"])):
            entry["end"] = end
        if obs.get("traceId"):
            entry["trace_ids"].add(obs["traceId"])
    return shipped


def _is_legacy(trace_id: str | None) -> bool:
    """Traces the pre-v4 shim wrote through the legacy ingestion API carry
    their readable key as id; OTel trace ids are 32 hex digits."""
    return bool(trace_id) and trace_id.startswith("ccusage-")


def _total(obs: dict[str, Any]) -> int:
    return sum(_tokens_from_usage_details(obs.get("usageDetails") or {})
               .values())


# --- the difference -> OTel spans --------------------------------------------

def _plan(units: list[dict[str, Any]],
          shipped: dict[tuple[str, tuple[str, str], str], dict[str, Any]],
          ) -> tuple[list[dict[str, Any]], list[str]]:
    """The generations to ship: per unit and model, what ccusage reports beyond
    what Langfuse holds. Returns (spans, warnings).

    A count below what was shipped (ccusage re-attributed tokens between
    models, or a log was rewritten) cannot be taken back without deleting the
    trace; it is reported, and that field ships nothing until ccusage catches
    up. `--reship` is the way to correct it."""
    spans: list[dict[str, Any]] = []
    warnings: list[str] = []
    for unit in units:
        for model, now in unit["models"].items():
            prev = shipped.get((unit["source"], unit["key"], model))
            before = prev["tokens"] if prev else dict.fromkeys(TOKEN_FIELDS, 0)
            delta = {f: now[f] - before[f] for f in TOKEN_FIELDS}
            if any(v < 0 for v in delta.values()):
                warnings.append(
                    f"{unit['trace_key']} {model}: Langfuse holds more than "
                    f"ccusage reports ({', '.join(f'{f} {v}' for f, v in delta.items() if v < 0)}); "
                    "run with --reship to correct it")
                delta = {f: max(v, 0) for f, v in delta.items()}
            if not any(delta.values()):
                continue
            # The new tokens were used after what was shipped last; dating the
            # generation from there puts its cost on the right day.
            start = unit["start"]
            if prev and prev["end"] and _ts(prev["end"]) > _ts(start):
                start = prev["end"]
            if _ts(start) > _ts(unit["end"]):
                start = unit["end"]
            spans.append({
                "unit": unit, "model": model, "start": start,
                "end": unit["end"], "usage": _usage_details(model, delta),
                # Deterministic per target state: a POST retried after a
                # timeout re-sends the same id, which _shipped counts once and
                # ClickHouse eventually merges.
                "span_id": _hex_id(
                    f"{unit['trace_key']}|{model}|"
                    + ",".join(str(now[f]) for f in TOKEN_FIELDS), 16),
            })
    return spans, warnings


def _hex_id(key: str, length: int) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:length]


def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _nanos(iso: str) -> str:
    return str(int(_ts(iso).timestamp() * 1_000_000) * 1000)


def _attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return {"key": key, "value": {"arrayValue": {
            "values": [{"stringValue": str(v)} for v in value]}}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _otel_span(span: dict[str, Any]) -> dict[str, Any]:
    """One OTLP/JSON span carrying a Langfuse generation.

    v4 queries observations directly, so the trace-level fields (user, session,
    name, tags, metadata) go on every span rather than on a trace record."""
    unit, model = span["unit"], span["model"]
    source = unit["source"]
    attrs = [
        _attr("langfuse.observation.type", "generation"),
        _attr("langfuse.observation.model.name", model),
        # No cost_details: Langfuse computes cost from these token counts and
        # its dated model prices, and leaves self-hosted models with no price
        # definition at 0 instead of a bogus number.
        _attr("langfuse.observation.usage_details", json.dumps(span["usage"])),
        _attr("langfuse.user.id", unit["metadata"]["host"]),
        _attr("langfuse.trace.name", f"ccusage:{source}"),
    ]
    if unit["session_id"]:
        attrs.append(_attr("langfuse.session.id", unit["session_id"]))
    if unit["tags"]:
        attrs.append(_attr("langfuse.trace.tags", unit["tags"]))
    for k, v in unit["metadata"].items():
        attrs.append(_attr(f"langfuse.trace.metadata.{k}", v))
        attrs.append(_attr(f"langfuse.observation.metadata.{k}", v))
    return {
        "traceId": unit["trace_id"],
        "spanId": span["span_id"],
        "name": f"{source}:{model}",
        "kind": 1,  # SPAN_KIND_INTERNAL
        "startTimeUnixNano": _nanos(span["start"]),
        "endTimeUnixNano": _nanos(span["end"]),
        "attributes": attrs,
    }


def _otlp_body(spans: list[dict[str, Any]]) -> dict[str, Any]:
    return {"resourceSpans": [{
        "resource": {"attributes": [_attr("service.name", "token-usage")]},
        "scopeSpans": [{
            "scope": {"name": "token-usage", "version": __version__},
            "spans": [_otel_span(s) for s in spans],
        }],
    }]}


# --- HTTP ---------------------------------------------------------------------

def _request(method: str, url: str, auth: tuple[str, str],
             **kwargs: Any) -> requests.Response:
    """One Langfuse API call, retrying transient network failures (the
    in-cluster gateway occasionally resets a connection mid-request). An HTTP
    error status is raised, not retried."""
    for attempt in range(4):
        try:
            resp = requests.request(method, url, auth=auth, timeout=60,
                                    **kwargs)
            break
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    if resp.status_code >= 400:
        sys.stderr.write(
            f"[ccusage-ship] {method} {url.split('/api/', 1)[-1]}: "
            f"HTTP {resp.status_code} {resp.text[:500]}\n")
        resp.raise_for_status()
    return resp


def _ship(host_url: str, auth: tuple[str, str],
          spans: list[dict[str, Any]]) -> None:
    # Chunked so a large backfill stays well under the request-size limit.
    for i in range(0, len(spans), 100):
        resp = _request("POST", f"{host_url}{OTLP_PATH}", auth,
                        json=_otlp_body(spans[i:i + 100]),
                        headers=INGESTION_HEADERS)
        try:
            partial = (resp.json() or {}).get("partialSuccess") or {}
        except ValueError:
            partial = {}
        if int(partial.get("rejectedSpans") or 0):
            raise requests.HTTPError(
                f"Langfuse rejected {partial['rejectedSpans']} span(s): "
                f"{partial.get('errorMessage', '')[:400]}")


def _delete_traces(host_url: str, auth: tuple[str, str], trace_ids: list[str],
                   host: str, from_start: str) -> None:
    """Delete traces and wait until Langfuse no longer returns them. Deletion
    is asynchronous; shipping into a trace whose deletion has not run yet
    would let it take the new spans with it."""
    for i in range(0, len(trace_ids), 100):
        _request("DELETE", f"{host_url}{TRACES_PATH}", auth,
                 json={"traceIds": trace_ids[i:i + 100]})
    pending = set(trace_ids)
    deadline = time.monotonic() + DELETE_TIMEOUT_S
    while True:
        present = {o.get("traceId") for o in
                   _iter_observations(host_url, auth, host, from_start)}
        pending &= present
        if not pending:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"{len(pending)} trace(s) still present after "
                f"{DELETE_TIMEOUT_S}s; not re-shipping them")
        time.sleep(10)


# --- moving the pre-v4 shim's traces over -------------------------------------

def _iter_legacy(host_url: str, auth: tuple[str, str], path: str,
                 params: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Page through a v3 list endpoint (`page`/`totalPages`)."""
    page = 1
    while True:
        resp = _request("GET", f"{host_url}{path}", auth,
                        params={**params, "page": page, "limit": 100})
        payload = resp.json()
        yield from payload.get("data") or []
        if page >= int((payload.get("meta") or {}).get("totalPages") or 0):
            return
        page += 1


def _legacy_units(host_url: str, auth: tuple[str, str],
                  host: str) -> list[dict[str, Any]]:
    """The pre-v4 shim's traces for `host`, read from Langfuse's v3 tables
    (complete, unlike their dual-written v4 copy) and shaped as units, so
    _plan can ship them like ccusage rows. Each carries the legacy trace id it
    replaces. The trace key is the legacy id itself, which is the formula
    _units uses: a session that keeps growing lands in the same trace."""
    gens: dict[str, list[dict[str, Any]]] = {}
    for obs in _iter_legacy(host_url, auth, "/api/public/observations",
                            {"userId": host, "type": "GENERATION"}):
        if _is_legacy(obs.get("traceId")):
            gens.setdefault(obs["traceId"], []).append(obs)
    units: list[dict[str, Any]] = []
    for trace in _iter_legacy(host_url, auth, TRACES_PATH, {"userId": host}):
        tid, name = trace.get("id"), trace.get("name") or ""
        if not _is_legacy(tid) or not name.startswith("ccusage:"):
            continue
        source = name.split(":", 1)[1]
        md = trace.get("metadata") or {}
        obs_list = gens.get(tid, [])
        start = trace.get("timestamp")
        ends = [o.get("endTime") or o.get("startTime") for o in obs_list]
        end = max([e for e in ends if e], key=_ts, default=start)
        if trace.get("sessionId"):
            key: tuple[str, str] = ("session", trace["sessionId"])
            metadata = {"host": host, "source": source,
                        "sessionId": trace["sessionId"]}
            if md.get("project"):
                metadata["project"] = md["project"]
        else:
            # A daily id ends in its date: ccusage-<host>-<source>-YYYY-MM-DD
            date = md.get("date") or tid[-10:]
            key = ("daily", date)
            metadata = {"host": host, "source": source, "date": date}
        models: dict[str, dict[str, int]] = {}
        for o in obs_list:
            counts = models.setdefault(o.get("model") or "unknown",
                                       dict.fromkeys(TOKEN_FIELDS, 0))
            for f, v in _tokens_from_usage_details(
                    o.get("usageDetails") or {}).items():
                counts[f] += v
        if not start or not models:
            continue
        if _ts(end) < _ts(start):
            end = start
        units.append({
            "source": source, "key": key, "trace_key": tid,
            "trace_id": _hex_id(tid, 32), "start": start, "end": end,
            "session_id": trace.get("sessionId"),
            "tags": list(trace.get("tags") or []), "metadata": metadata,
            "models": models, "legacy_trace_id": tid,
        })
    return units


def _wait_visible(host_url: str, auth: tuple[str, str], host: str,
                  from_start: str, span_ids: set[str]) -> None:
    deadline = time.monotonic() + 300
    while True:
        seen = {o.get("id") for o in
                _iter_observations(host_url, auth, host, from_start)}
        if span_ids <= seen:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"{len(span_ids - seen)} migrated "
                               "generation(s) not visible after 300s")
        time.sleep(5)


def _migrate_legacy(host_url: str, auth: tuple[str, str], host: str,
                    dry_run: bool) -> list[dict[str, Any]]:
    """Move this host's pre-v4 traces onto OTel: ship what the v4 data does
    not hold yet, wait until Langfuse returns it, then delete the legacy
    traces. Re-runnable at any point — a run cut short ships nothing twice
    and deletes nothing that was not re-sent first. Returns the
    generations shipped (in a dry run: the ones that would be).

    Needs Langfuse's v3 read API, which exists only while the server runs
    the `legacy` or `dual` write mode; after the cutover to `events_only`
    there is nothing left to move."""
    try:
        units = _legacy_units(host_url, auth, host)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        if code in (400, 404, 405, 410, 501):
            return []
        raise
    if not units:
        return []
    from_start = _window_start(units, "9999-12-31")
    # Warnings are expected and dropped: a session that grew after the
    # pre-v4 shim's last run holds more on OTel than its legacy snapshot.
    spans, _ = _plan(units, _shipped(
        _iter_observations(host_url, auth, host, from_start)))
    doomed = sorted(u["legacy_trace_id"] for u in units)
    if dry_run:
        print(json.dumps({"migrate_delete_legacy_traces": doomed,
                          **_otlp_body(spans)}, indent=2))
        sys.stderr.write(f"[ccusage-ship] migrate: would move "
                         f"{len(doomed)} legacy trace(s), shipping "
                         f"{len(spans)} generation(s)\n")
        return spans
    if spans:
        _ship(host_url, auth, spans)
        _wait_visible(host_url, auth, host, from_start,
                      {s["span_id"] for s in spans})
    for i in range(0, len(doomed), 100):
        _request("DELETE", f"{host_url}{TRACES_PATH}", auth,
                 json={"traceIds": doomed[i:i + 100]})
    sys.stderr.write(f"[ccusage-ship] migrate: moved {len(doomed)} legacy "
                     f"trace(s) to OTel, shipped {len(spans)} "
                     "generation(s)\n")
    return spans


def _as_observation(span: dict[str, Any]) -> dict[str, Any]:
    """A planned span as the Observations API would return it, so a dry run
    can count the migration it did not perform."""
    unit = span["unit"]
    return {"id": span["span_id"], "traceId": unit["trace_id"],
            "model": span["model"], "sessionId": unit["session_id"],
            "traceName": f"ccusage:{unit['source']}",
            "metadata": unit["metadata"], "usageDetails": span["usage"],
            "startTime": span["start"], "endTime": span["end"]}


# --- run ----------------------------------------------------------------------

class _Lock:
    """One run at a time per user: two overlapping runs would both see the
    same gap and ship it twice. A lock older than two hours is from a run
    that died, and is taken over."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                self.fd = os.open(self.path,
                                  os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return True
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age < 2 * 3600:
                    return False
                self.path.unlink(missing_ok=True)
        return False

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.path.unlink(missing_ok=True)
            self.fd = None


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="token-usage",
        description="Ship ccusage usage (per session by default) to a "
                    "self-hosted Langfuse v4 instance. Ships only what "
                    "Langfuse does not hold yet.")
    window = p.add_mutually_exclusive_group()
    window.add_argument(
        "--since-days", type=int, metavar="N", default=None,
        help="Compare the last N days. Overrides CCUSAGE_SINCE_DAYS. "
             "Handy for one-off backfills, e.g. --since-days 300.")
    window.add_argument(
        "--since", metavar="YYYY-MM-DD", default=None,
        help="Compare since this exact date. Overrides --since-days and "
             "CCUSAGE_SINCE_DAYS.")
    p.add_argument(
        "--granularity", choices=["session", "daily"], default=None,
        help="Group traces by session (default) or by day. Overrides "
             "CCUSAGE_GRANULARITY. Session shows which session/project drove "
             "the spend; daily is one trace per calendar day.")
    p.add_argument(
        "--reship", action="store_true",
        help="Delete the traces of every session in the window that ccusage "
             "still has, then ship those sessions in full. For correcting "
             "data already in Langfuse.")
    p.add_argument(
        "--migrate-host", metavar="HOST", default=None,
        help="Only move HOST's pre-v4 traces onto OTel, then exit. Every run "
             "does this for its own host; this is for machines that no "
             "longer run token-usage.")
    p.add_argument(
        "--version", action="store_true",
        help="Print the token-usage version and the ccusage it runs, then "
             "exit. Needs no configuration.")
    p.add_argument(
        "--self-test", action="store_true",
        help="Send a canary trace under a throwaway host, read it back, "
             "check every field Langfuse mapped, and delete it. Run after "
             "a Langfuse upgrade.")
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be shipped (and deleted) and change nothing "
             "(same as TOKEN_USAGE_DRY_RUN=1).")
    return p.parse_args(argv)


class _Timestamped:
    """stderr with the local time at the start of every line.

    The Windows Scheduled Task and the clone-based Linux timer append the
    shim's output to ship.log, where hourly runs are otherwise
    indistinguishable. A terminal and journald (which stamps lines itself and
    announces itself through JOURNAL_STREAM) get the lines unchanged."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.at_line_start = True

    def write(self, text: str) -> int:
        out = []
        for chunk in text.splitlines(keepends=True):
            if self.at_line_start:
                out.append(datetime.now().astimezone()
                           .strftime("%Y-%m-%d %H:%M:%S%z "))
            out.append(chunk)
            self.at_line_start = chunk.endswith("\n")
        self.stream.write("".join(out))
        return len(text)

    def flush(self) -> None:
        self.stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


def _stamp_stderr() -> None:
    stream = sys.stderr
    if stream is None or os.environ.get("JOURNAL_STREAM"):
        return
    try:
        if stream.isatty():
            return
    except (AttributeError, ValueError):
        pass
    sys.stderr = _Timestamped(stream)


def _versions() -> str:
    """This shim's version and the ccusage it would run. The ccusage half is
    what tells a working install from one whose Node lost its global
    packages."""
    try:
        exe = _ccusage_exe()
    except FileNotFoundError:
        return f"token-usage {__version__}\nccusage not found on PATH"
    try:
        out = subprocess.run([exe, "--version"], capture_output=True,
                             text=True, timeout=60, creationflags=_NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"token-usage {__version__}\nccusage {exe}: {exc}"
    if out.returncode != 0:
        return (f"token-usage {__version__}\n"
                f"ccusage {exe}: exited {out.returncode}")
    version = (out.stdout.split() or ["?"])[-1]
    return f"token-usage {__version__}\nccusage {version} ({exe})"


def _ping_heartbeat(url: str) -> None:
    """Best-effort success ping to a dead-man's-switch monitor (Uptime Kuma
    push, healthchecks.io, …). Called only after a fully successful run, so a
    missing ping — the timer never fired, ccusage broke, or Langfuse was
    unreachable — is what trips the monitor's alert. Never fails the run: a
    heartbeat that can't be reached is logged and swallowed."""
    try:
        requests.get(url, timeout=10).raise_for_status()
    except requests.RequestException as exc:
        sys.stderr.write(f"[ccusage-ship] heartbeat ping failed: {exc}\n")


def _self_test(langfuse: str, auth: tuple[str, str], host: str) -> int:
    """Round-trip a canary through Langfuse and compare what comes back with
    what was sent. The OTel attribute names are Langfuse's to change; this is
    what notices when an upgrade stops mapping one of them."""
    canary_host = f"token-usage-selftest-{host}"
    sid = f"selftest-{int(time.time())}"
    started = datetime.now(timezone.utc) - timedelta(days=2)
    row = {"sessionId": sid, "projectPath": "-home-x-source-selftest",
           "firstActivity": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "lastActivity": (started + timedelta(hours=1))
           .strftime("%Y-%m-%dT%H:%M:%SZ"),
           "modelBreakdowns": [{"modelName": "claude-opus-4-8",
                                "inputTokens": 11, "outputTokens": 22,
                                "cacheCreationTokens": 33,
                                "cacheReadTokens": 44}]}
    units = _units([row], canary_host, "selftest", "session")
    spans, _ = _plan(units, {})
    trace_id = units[0]["trace_id"]
    from_start = _window_start(units, started.date().isoformat())
    problems: list[str] = []
    try:
        _ship(langfuse, auth, spans)
        obs: list[dict[str, Any]] = []
        deadline = time.monotonic() + 120
        while not obs and time.monotonic() < deadline:
            time.sleep(3)
            obs = [o for o in _iter_observations(langfuse, auth, canary_host,
                                                 from_start)
                   if o.get("traceId") == trace_id]
        if not obs:
            problems.append("canary not visible after 120s (is the "
                            "x-langfuse-ingestion-version header honoured?)")
        else:
            o = obs[0]
            md = o.get("metadata") or {}
            expected = {
                "type": (o.get("type"), "GENERATION"),
                "model": (o.get("model"), "claude-opus-4-8"),
                "userId": (o.get("userId"), canary_host),
                "sessionId": (o.get("sessionId"), sid),
                "traceName": (o.get("traceName"), "ccusage:selftest"),
                "tags": (o.get("tags"), ["project:selftest"]),
                "metadata.source": (md.get("source"), "selftest"),
                "metadata.sessionId": (md.get("sessionId"), sid),
                "startTime": (o.get("startTime", "")[:16],
                              row["firstActivity"][:16]),
                "tokens": (_tokens_from_usage_details(
                    o.get("usageDetails") or {}),
                    {f: units[0]["models"]["claude-opus-4-8"][f]
                     for f in TOKEN_FIELDS}),
            }
            problems += [f"{k}: got {got!r}, expected {want!r}"
                         for k, (got, want) in expected.items()
                         if got != want]
            if not o.get("totalCost"):
                problems.append("totalCost is 0: Langfuse did not price "
                                "the usage keys")
    finally:
        try:
            _delete_traces(langfuse, auth, [trace_id], canary_host,
                           from_start)
        except (requests.RequestException, TimeoutError) as exc:
            problems.append(f"canary trace {trace_id} not deleted: {exc}")
    for p in problems:
        sys.stderr.write(f"[ccusage-ship] self-test: {p}\n")
    if not problems:
        sys.stderr.write("[ccusage-ship] self-test: OK\n")
    return 2 if problems else 0


def _window_start(units: list[dict[str, Any]], since: str) -> str:
    """Where to start reading Langfuse: the earliest unit start (a session
    reported for the window may have begun long before it), a day earlier for
    clock skew between the machines' local and UTC timestamps."""
    earliest = min([_ts(u["start"]) for u in units]
                   + [_ts(f"{since}T00:00:00Z")])
    return (earliest - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_source(source: str, rows: list[dict[str, Any]], *, host: str,
                granularity: str, since: str, langfuse: str,
                auth: tuple[str, str], reship: bool, dry_run: bool,
                pending: list[dict[str, Any]] | None = None) -> None:
    units = _units(rows, host, source, granularity)
    if not units:
        sys.stderr.write(f"[ccusage-ship] {source}: nothing in the window\n")
        return
    from_start = _window_start(units, since)
    held = list(_iter_observations(langfuse, auth, host, from_start))
    held += [_as_observation(s) for s in pending or []]
    shipped = {k: v for k, v in _shipped(iter(held)).items()
               if k[0] == source}

    if reship:
        keys = {u["key"] for u in units}
        doomed = sorted({tid for (_, key, _), e in shipped.items()
                         if key in keys for tid in e["trace_ids"]})
        if dry_run:
            print(json.dumps({"source": source, "delete_traces": doomed},
                             indent=2))
            shipped = {}
        elif doomed:
            sys.stderr.write(f"[ccusage-ship] {source}: deleting "
                             f"{len(doomed)} trace(s) to re-ship\n")
            _delete_traces(langfuse, auth, doomed, host, from_start)
            shipped = {k: v for k, v in _shipped(
                _iter_observations(langfuse, auth, host, from_start)).items()
                if k[0] == source}

    spans, warnings = _plan(units, shipped)
    for w in warnings:
        sys.stderr.write(f"[ccusage-ship] {source}: {w}\n")
    if dry_run:
        print(json.dumps(_otlp_body(spans), indent=2))
    elif spans:
        _ship(langfuse, auth, spans)
    unit = "session" if granularity == "session" else "day"
    verb = "would ship" if dry_run else "shipped"
    sys.stderr.write(
        f"[ccusage-ship] {source}: {len(units)} {unit}(s) in the window, "
        f"{verb} {len(spans)} generation(s)\n")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    _stamp_stderr()
    if args.version:
        print(_versions())
        return 0
    _load_dotenv_best_effort()
    langfuse, pk, sk, sources = _require_env()
    dry_run = args.dry_run or os.environ.get("TOKEN_USAGE_DRY_RUN") == "1"
    host = os.environ.get("TOKEN_USAGE_HOSTNAME") or socket.gethostname()
    if args.self_test:
        try:
            return _self_test(langfuse, (pk, sk), host)
        except requests.RequestException as exc:
            sys.stderr.write(f"[ccusage-ship] self-test: failed: {exc}\n")
            return 2
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

    lock = _Lock(_state_dir() / "ship.lock")
    if not dry_run and not lock.acquire():
        sys.stderr.write(f"[ccusage-ship] another run holds {lock.path}; "
                         "exiting\n")
        return 1
    failures: list[str] = []
    try:
        if args.migrate_host:
            try:
                _migrate_legacy(langfuse, (pk, sk), args.migrate_host,
                                dry_run)
                return 0
            except (requests.RequestException, TimeoutError) as exc:
                sys.stderr.write(f"[ccusage-ship] migrate: failed: {exc}\n")
                return 2
        # Before anything else: _shipped ignores legacy traces, so a source
        # compared while they are still there would ship them a second time.
        try:
            migrated = _migrate_legacy(langfuse, (pk, sk), host, dry_run)
        except (requests.RequestException, TimeoutError) as exc:
            sys.stderr.write(f"[ccusage-ship] migrate: failed: {exc}; "
                             "shipping nothing this run\n")
            return 2
        for source in sources:
            try:
                rows = _ccusage_rows(source, since.replace("-", ""),
                                     granularity)
            except subprocess.CalledProcessError as exc:
                sys.stderr.write(f"[ccusage-ship] {source}: ccusage exited "
                                 f"{exc.returncode}\n")
                failures.append(source)
                continue
            except (json.JSONDecodeError, FileNotFoundError) as exc:
                sys.stderr.write(f"[ccusage-ship] {source}: {exc}\n")
                failures.append(source)
                continue
            try:
                _run_source(source, rows, host=host, granularity=granularity,
                            since=since, langfuse=langfuse, auth=(pk, sk),
                            reship=args.reship, dry_run=dry_run,
                            pending=migrated if dry_run else None)
            except (requests.RequestException, TimeoutError, ValueError) as exc:
                sys.stderr.write(f"[ccusage-ship] {source}: failed: {exc}\n")
                failures.append(source)
    finally:
        lock.release()

    # Heartbeat only on a fully clean run, so the monitor alerts on any failure
    # (including the timer not firing at all). Skipped in dry-run.
    if not failures and not dry_run:
        heartbeat = os.environ.get("HEARTBEAT_URL")
        if heartbeat:
            _ping_heartbeat(heartbeat)
    return 2 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
