"""Tests for the Langfuse batch format produced by ccusage-ship.py.

These guard the exact shape the dashboard depends on — usageDetails (incl.
cache tokens), no costDetails (Langfuse computes cost), the real model name for
breakdown-less sources like codex, deterministic ids, and slim tags. They would
have caught the codex=$0 and cache-undercount regressions we hit in practice.
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


def _claude_row():
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


def _codex_row():
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


def test_trace_id_and_tags_are_deterministic_and_slim():
    batch = ship._build_batch([_claude_row()], HOST, "claude")
    traces, _ = _split(batch)
    assert len(traces) == 1
    body = traces[0]["body"]
    assert body["id"] == "ccusage-test-host-claude-2026-06-10"
    # tags are host + source only — no per-day date: tag flooding the picker
    assert body["tags"] == [HOST, "claude"]
    assert not any(str(t).startswith("date:") for t in body["tags"])


def test_generation_uses_usagedetails_with_cache_and_no_costdetails():
    batch = ship._build_batch([_claude_row()], HOST, "claude")
    _, gens = _split(batch)
    assert len(gens) == 1
    body = gens[0]["body"]
    assert body["model"] == "claude-opus-4-8"
    assert set(body["usageDetails"]) == USAGE_KEYS
    assert body["usageDetails"]["cache_read_input_tokens"] == 4000
    assert body["usageDetails"]["cache_creation_input_tokens"] == 300
    # cost is Langfuse's job now — never ship costDetails or the legacy block
    assert "costDetails" not in body
    assert "usage" not in body


def test_codex_uses_real_model_name_so_langfuse_can_price_it():
    batch = ship._build_batch([_codex_row()], HOST, "codex")
    _, gens = _split(batch)
    assert len(gens) == 1
    body = gens[0]["body"]
    # the model is gpt-5.5 (from the models dict), NOT "codex"/"aggregate"
    assert body["model"] == "gpt-5.5"
    assert body["id"] == "ccusage-test-host-codex-2026-06-10-gen-gpt-5.5"
    assert body["usageDetails"]["cache_read_input_tokens"] == 60


def test_falls_back_to_source_when_no_model_name_anywhere():
    row = {"date": "2026-06-10", "inputTokens": 1, "outputTokens": 2}
    batch = ship._build_batch([row], HOST, "mystery")
    _, gens = _split(batch)
    assert gens[0]["body"]["model"] == "mystery"


def test_one_generation_per_model_breakdown():
    row = _claude_row()
    row["modelBreakdowns"].append({
        "modelName": "claude-haiku-4-5",
        "inputTokens": 1, "outputTokens": 2,
        "cacheCreationTokens": 0, "cacheReadTokens": 0, "cost": 0.01,
    })
    batch = ship._build_batch([row], HOST, "claude")
    _, gens = _split(batch)
    assert {g["body"]["model"] for g in gens} == {
        "claude-opus-4-8", "claude-haiku-4-5"}


def test_rows_without_date_are_skipped():
    assert ship._build_batch([{"inputTokens": 1}], HOST, "claude") == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
