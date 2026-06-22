"""Tests for the Langfuse batch format produced by ccusage-ship.py.

These guard the exact shape the dashboard depends on — usageDetails (incl.
cache tokens), no costDetails (Langfuse computes cost), the real model name for
breakdown-less sources like codex, deterministic ids, and slim tags — for both
the default session granularity and daily.
"""
import importlib.util
from pathlib import Path

import pytest

# The module file name has a hyphen, so it can't be imported normally.
_spec = importlib.util.spec_from_file_location(
    "ccusage_ship", Path(__file__).parent / "ccusage-ship.py")
ship = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ship)

HOST = "test-host"
USAGE_KEYS = {
    "input", "output",
    "cache_creation_input_tokens", "cache_read_input_tokens",
}


def _daily_row():
    return {
        "date": "2026-06-10",
        "modelsUsed": ["claude-opus-4-8"],
        "modelBreakdowns": [{
            "modelName": "claude-opus-4-8",
            "inputTokens": 100, "outputTokens": 200,
            "cacheCreationTokens": 300, "cacheReadTokens": 4000,
            "cost": 1.23,
        }],
        "cacheCreationTokens": 300, "cacheReadTokens": 4000,
    }


def _session_row():
    return {
        "sessionId": "abc-123",
        "projectPath": "D--source-whisper-dictate",
        "firstActivity": "2026-06-10T05:00:00.000Z",
        "lastActivity": "2026-06-10T06:30:00.000Z",
        "modelBreakdowns": [{
            "modelName": "claude-opus-4-8",
            "inputTokens": 100, "outputTokens": 200,
            "cacheCreationTokens": 300, "cacheReadTokens": 4000,
        }],
    }


def _codex_daily_row():
    # codex has no modelBreakdowns; the real model lives in a `models` dict.
    return {
        "date": "2026-06-10",
        "models": {"gpt-5.5": {}},
        "inputTokens": 10, "outputTokens": 20,
        "cacheCreationTokens": 5, "cacheReadTokens": 60,
    }


def _split(batch):
    traces = [e for e in batch if e["type"] == "trace-create"]
    gens = [e for e in batch if e["type"] == "generation-create"]
    return traces, gens


# --- session (default) -----------------------------------------------------

def test_session_is_the_default_granularity():
    # No granularity arg -> session: a row with only a date and no sessionId
    # is skipped, while a session row is shipped.
    assert ship._build_batch([_daily_row()], HOST, "claude") == []
    assert ship._build_batch([_session_row()], HOST, "claude") != []


def test_session_maps_to_native_langfuse_fields():
    batch = ship._build_batch([_session_row()], HOST, "claude")
    traces, gens = _split(batch)
    body = traces[0]["body"]
    assert body["id"] == "ccusage-test-host-claude-sess-abc-123"
    assert body["timestamp"] == "2026-06-10T05:00:00.000Z"  # firstActivity
    # host -> userId, session -> sessionId, source -> name (all native fields)
    assert body["userId"] == HOST
    assert body["sessionId"] == "abc-123"
    assert body["name"] == "ccusage:claude"
    # the only tag is the machine-independent project basename; host/source are
    # NOT tags anymore
    assert body["tags"] == ["project:whisper-dictate"]
    assert HOST not in body["tags"] and "claude" not in body["tags"]
    # full path kept in metadata for disambiguation
    assert body["metadata"]["project"] == "D--source-whisper-dictate"
    assert body["metadata"]["sessionId"] == "abc-123"
    g = gens[0]["body"]
    assert g["id"] == "ccusage-test-host-claude-sess-abc-123-gen-claude-opus-4-8"
    assert g["startTime"] == "2026-06-10T05:00:00.000Z"
    assert g["endTime"] == "2026-06-10T06:30:00.000Z"
    assert set(g["usageDetails"]) == USAGE_KEYS
    assert g["usageDetails"]["cache_read_input_tokens"] == 4000
    assert "costDetails" not in g and "usage" not in g


def test_session_without_id_is_skipped():
    assert ship._build_batch([{"firstActivity": "x"}], HOST, "claude") == []


# --- daily -----------------------------------------------------------------

def test_daily_trace_id_userid_and_no_tags():
    batch = ship._build_batch([_daily_row()], HOST, "claude", "daily")
    traces, _ = _split(batch)
    body = traces[0]["body"]
    assert body["id"] == "ccusage-test-host-claude-2026-06-10"
    assert body["userId"] == HOST          # host is a userId, not a tag
    assert body["tags"] == []              # daily has no project, no tags
    assert "sessionId" not in body         # daily isn't a session


@pytest.mark.parametrize("path,expected", [
    ("D--source-whisper-dictate", "whisper-dictate"),
    ("-home-lars-source-homelab", "homelab"),          # same project, WSL2
    ("-home-lars-source-flux-home", "flux-home"),       # hyphen in name kept
    ("-home-lars-source-homelab/abc/subagents", "homelab"),  # subagent path
    ("home-lars/43f5-uuid/subagents/workflows", "home-lars"),  # no source +
    ("--home-lars-source-flux-home--", "flux-home"),    # leading/trailing --
    ("--home-lars-source-voice-pi--", "voice-pi"),      # trailing slash -> --
    ("--tmp--", "tmp"),                                  # no `source`, stripped
    ("C--Users-larsm", "C--Users-larsm"),               # no `source` -> raw
    (None, None),
])
def test_project_label_unifies_across_machines(path, expected):
    assert ship._project_label(path) == expected


def test_daily_generation_usagedetails_no_costdetails():
    batch = ship._build_batch([_daily_row()], HOST, "claude", "daily")
    _, gens = _split(batch)
    body = gens[0]["body"]
    assert body["model"] == "claude-opus-4-8"
    assert set(body["usageDetails"]) == USAGE_KEYS
    assert "costDetails" not in body and "usage" not in body


def test_codex_uses_real_model_name_so_langfuse_can_price_it():
    batch = ship._build_batch([_codex_daily_row()], HOST, "codex", "daily")
    _, gens = _split(batch)
    body = gens[0]["body"]
    assert body["model"] == "gpt-5.5"  # not "codex"/"aggregate"
    assert body["usageDetails"]["cache_read_input_tokens"] == 60


def test_falls_back_to_source_when_no_model_name_anywhere():
    row = {"date": "2026-06-10", "inputTokens": 1, "outputTokens": 2}
    batch = ship._build_batch([row], HOST, "mystery", "daily")
    _, gens = _split(batch)
    assert gens[0]["body"]["model"] == "mystery"


def test_one_generation_per_model_breakdown():
    row = _daily_row()
    row["modelBreakdowns"].append({
        "modelName": "claude-haiku-4-5",
        "inputTokens": 1, "outputTokens": 2,
        "cacheCreationTokens": 0, "cacheReadTokens": 0, "cost": 0.01,
    })
    _, gens = _split(ship._build_batch([row], HOST, "claude", "daily"))
    assert {g["body"]["model"] for g in gens} == {
        "claude-opus-4-8", "claude-haiku-4-5"}


def test_daily_rows_without_date_are_skipped():
    assert ship._build_batch([{"inputTokens": 1}], HOST, "claude", "daily") == []


def test_ccusage_subprocess_is_windowless_on_windows():
    # ccusage spawns under pythonw for the silent Scheduled Task; on Windows the
    # subprocess must carry CREATE_NO_WINDOW, and be a harmless 0 elsewhere.
    import sys
    if sys.platform == "win32":
        assert ship._NO_WINDOW == ship.subprocess.CREATE_NO_WINDOW
    else:
        assert ship._NO_WINDOW == 0


def test_heartbeat_pings_url_and_swallows_errors(monkeypatch):
    # success ping hits exactly the configured URL
    calls = []

    class _Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(ship.requests, "get",
                        lambda url, timeout=None: calls.append(url) or _Resp())
    ship._ping_heartbeat("https://hc.example/abc")
    assert calls == ["https://hc.example/abc"]

    # a broken monitor must never propagate out of the run
    def _boom(*a, **k):
        raise ship.requests.RequestException("monitor down")

    monkeypatch.setattr(ship.requests, "get", _boom)
    ship._ping_heartbeat("https://hc.example/abc")  # must not raise


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
