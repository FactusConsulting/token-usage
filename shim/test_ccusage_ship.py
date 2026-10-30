"""Tests for ccusage-ship.py.

These guard what the dashboard and Langfuse v4's append-only storage depend
on: the OTel attributes each generation carries (usage keys per provider, no
cost, the real model name, user/session/tags/metadata on every span), and the
comparison with what Langfuse already holds — only the difference is shipped,
never a record twice.
"""
import importlib.util
import json
from pathlib import Path

import pytest

# The module file name has a hyphen, so it can't be imported normally.
_spec = importlib.util.spec_from_file_location(
    "ccusage_ship", Path(__file__).parent / "ccusage-ship.py")
ship = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ship)

HOST = "test-host"


def _session_row(**tokens):
    counts = {"inputTokens": 100, "outputTokens": 200,
              "cacheCreationTokens": 300, "cacheReadTokens": 4000, **tokens}
    return {
        "sessionId": "abc-123",
        "projectPath": "D--source-whisper-dictate",
        "firstActivity": "2026-06-10T05:00:00.000Z",
        "lastActivity": "2026-06-10T06:30:00.000Z",
        "modelBreakdowns": [{"modelName": "claude-opus-4-8", **counts}],
    }


def _daily_row():
    return {
        "date": "2026-06-10",
        "modelBreakdowns": [{
            "modelName": "claude-opus-4-8",
            "inputTokens": 100, "outputTokens": 200,
            "cacheCreationTokens": 300, "cacheReadTokens": 4000,
        }],
    }


def _codex_session_row():
    # codex: no firstActivity and no modelBreakdowns; the start is in the
    # rollout file name and the model in a `models` dict.
    return {
        "sessionId": "2026/05/28/rollout-2026-05-28T19-05-55-019e6f8c",
        "lastActivity": "2026-05-28T20:00:00.000Z",
        "models": {"gpt-5.5": {"inputTokens": 10, "outputTokens": 20,
                               "cacheCreationTokens": 0,
                               "cacheReadTokens": 60}},
        "inputTokens": 10, "outputTokens": 20, "cacheReadTokens": 60,
    }


def _spans(rows, source="claude", granularity="session", shipped=None):
    units = ship._units(rows, HOST, source, granularity)
    spans, warnings = ship._plan(units, shipped or {})
    return spans, warnings


def _attrs(span):
    out = {}
    for a in ship._otel_span(span)["attributes"]:
        v = a["value"]
        out[a["key"]] = ([x["stringValue"] for x in v["arrayValue"]["values"]]
                         if "arrayValue" in v else v["stringValue"])
    return out


def _obs(trace_id, usage, *, oid="o1", model="claude-opus-4-8",
         source="claude", session="abc-123", end="2026-06-10T06:00:00.000Z"):
    """A generation as the Observations API v2 returns it."""
    return {"id": oid, "traceId": trace_id, "model": model,
            "traceName": f"ccusage:{source}", "sessionId": session,
            "startTime": "2026-06-10T05:00:00.000Z", "endTime": end,
            "metadata": {"source": source, "sessionId": session},
            "usageDetails": usage}


# --- what one generation carries ---------------------------------------------

def test_session_generation_carries_trace_fields_on_the_span():
    spans, _ = _spans([_session_row()])
    assert len(spans) == 1
    a = _attrs(spans[0])
    assert a["langfuse.observation.type"] == "generation"
    assert a["langfuse.observation.model.name"] == "claude-opus-4-8"
    # v4 queries observations directly: user, session, name, tags and
    # metadata must be on the span itself, not only on a trace record.
    assert a["langfuse.user.id"] == HOST
    assert a["langfuse.session.id"] == "abc-123"
    assert a["langfuse.trace.name"] == "ccusage:claude"
    assert a["langfuse.trace.tags"] == ["project:whisper-dictate"]
    assert a["langfuse.observation.metadata.source"] == "claude"
    assert a["langfuse.observation.metadata.sessionId"] == "abc-123"
    assert a["langfuse.observation.metadata.project"] == \
        "D--source-whisper-dictate"
    assert json.loads(a["langfuse.observation.usage_details"]) == {
        "input": 100, "output": 200, "cache_creation_input_tokens": 300,
        "cache_read_input_tokens": 4000}
    assert not any("cost" in k for k in a)  # Langfuse prices it


def test_span_ids_and_times_are_valid_otlp():
    span = ship._otel_span(_spans([_session_row()])[0][0])
    assert len(span["traceId"]) == 32 and len(span["spanId"]) == 16
    int(span["traceId"], 16), int(span["spanId"], 16)
    assert span["startTimeUnixNano"] == "1781067600000000000"
    assert int(span["endTimeUnixNano"]) > int(span["startTimeUnixNano"])


def test_ids_are_deterministic():
    a, _ = _spans([_session_row()])
    b, _ = _spans([_session_row()])
    assert a[0]["unit"]["trace_id"] == b[0]["unit"]["trace_id"]
    assert a[0]["span_id"] == b[0]["span_id"]
    # a new target state is a new generation, not the same id re-sent
    c, _ = _spans([_session_row(outputTokens=999)])
    assert c[0]["span_id"] != a[0]["span_id"]


def test_daily_has_no_session_and_no_tags():
    spans, _ = _spans([_daily_row()], granularity="daily")
    a = _attrs(spans[0])
    assert "langfuse.session.id" not in a and "langfuse.trace.tags" not in a
    assert a["langfuse.observation.metadata.date"] == "2026-06-10"
    assert spans[0]["start"] == "2026-06-10T00:00:00Z"


def test_codex_start_from_rollout_name_and_real_model():
    spans, _ = _spans([_codex_session_row()], source="codex")
    assert spans[0]["start"] == "2026-05-28T19:05:55Z"
    assert spans[0]["model"] == "gpt-5.5"
    # OpenAI models are priced under input_cache_read; Anthropic's key name
    # would leave these tokens worth $0.
    assert spans[0]["usage"] == {"input": 10, "output": 20,
                                 "input_cache_read": 60}


def test_rows_without_identity_are_skipped():
    assert _spans([{"firstActivity": "2026-06-10T05:00:00Z"}])[0] == []
    assert _spans([{"inputTokens": 1}], granularity="daily")[0] == []


def test_one_generation_per_model():
    row = _session_row()
    row["modelBreakdowns"].append({"modelName": "claude-haiku-4-5",
                                   "inputTokens": 1, "outputTokens": 2})
    spans, _ = _spans([row])
    assert {s["model"] for s in spans} == {"claude-opus-4-8",
                                           "claude-haiku-4-5"}


def test_falls_back_to_source_when_no_model_name_anywhere():
    row = {"date": "2026-06-10", "inputTokens": 1, "outputTokens": 2}
    spans, _ = _spans([row], source="mystery", granularity="daily")
    assert spans[0]["model"] == "mystery"


@pytest.mark.parametrize("source,expected", [
    # Langfuse matches its price table on the bare id; a labelled name is $0.
    ("omp", "claude-opus-5-5"),
    # pi's data is stored under the labelled name; renaming would re-ship it.
    ("pi", "[pi] claude-opus-5-5"),
])
def test_omp_ships_the_bare_model_id_pi_keeps_its_label(source, expected):
    row = _session_row()
    row["modelBreakdowns"][0]["modelName"] = "[pi] claude-opus-5-5"
    spans, _ = _spans([row], source=source)
    assert spans[0]["model"] == expected


@pytest.mark.parametrize("model,expected", [
    ("claude-opus-5", "cache_read_input_tokens"),
    ("claude-haiku-4-5-20251001", "cache_read_input_tokens"),
    ("anthropic.claude-opus-4-8", "cache_read_input_tokens"),
    ("anthropic/claude-sonnet-5", "cache_read_input_tokens"),
    ("gpt-5.5", "input_cache_read"),
    ("gpt-5.6-sol", "input_cache_read"),
    ("gpt-5.3-codex-spark", "input_cache_read"),
    ("openai/gpt-5", "input_cache_read"),
])
def test_cache_read_key_matches_the_provider_langfuse_prices(model, expected):
    """Langfuse prices cache reads under a provider-specific key, and a key it
    does not price contributes $0 while still showing up as tokens. Emitting
    the wrong one is silent, so pin the mapping."""
    details = ship._usage_details(model, {"cacheReadTokens": 7})
    assert details == {expected: 7}


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


# --- shipping only the difference ---------------------------------------------

def _trace_id():
    return ship._units([_session_row()], HOST, "claude", "session")[0][
        "trace_id"]


def test_nothing_shipped_when_langfuse_is_up_to_date():
    held = ship._shipped(iter([_obs(_trace_id(), {
        "input": 100, "output": 200, "cache_creation_input_tokens": 300,
        "cache_read_input_tokens": 4000, "total": 4600})]))
    assert _spans([_session_row()], shipped=held) == ([], [])


def test_only_the_growth_is_shipped_dated_from_the_last_end():
    held = ship._shipped(iter([_obs(_trace_id(), {
        "input": 60, "output": 200, "cache_creation_input_tokens": 300,
        "cache_read_input_tokens": 1000})]))
    spans, _ = _spans([_session_row()], shipped=held)
    assert spans[0]["usage"] == {"input": 40, "cache_read_input_tokens": 3000}
    assert spans[0]["start"] == "2026-06-10T06:00:00.000Z"  # previous end
    assert spans[0]["end"] == "2026-06-10T06:30:00.000Z"


def test_deltas_from_several_generations_add_up():
    tid = _trace_id()
    held = ship._shipped(iter([
        _obs(tid, {"input": 50, "output": 100}, oid="a"),
        _obs(tid, {"input": 50, "output": 100,
                   "cache_creation_input_tokens": 300,
                   "cache_read_input_tokens": 4000}, oid="b")]))
    assert _spans([_session_row()], shipped=held) == ([], [])


def test_rows_sharing_an_id_count_once():
    """The pre-v4 shim re-sent the same id hourly; v4 keeps every copy until
    they are cleaned up. Counting them all would ship nothing ever again."""
    tid = _trace_id()
    full = {"input": 100, "output": 200, "cache_creation_input_tokens": 300,
            "cache_read_input_tokens": 4000}
    older = {"input": 90, "output": 200, "cache_creation_input_tokens": 300,
             "cache_read_input_tokens": 3000}
    held = ship._shipped(iter([_obs(tid, older), _obs(tid, full),
                               _obs(tid, full)]))
    key = ("claude", ("session", "abc-123"), "claude-opus-4-8")
    assert held[key]["tokens"]["cacheReadTokens"] == 4000
    assert _spans([_session_row()], shipped=held) == ([], [])


def test_legacy_cache_read_key_counts_for_openai_models():
    """codex data shipped before the per-provider key carries Anthropic's
    cache-read name; it is still tokens already shipped."""
    row = _codex_session_row()
    units = ship._units([row], HOST, "codex", "session")
    held = ship._shipped(iter([_obs(
        units[0]["trace_id"], {"input": 10, "output": 20,
                               "cache_read_input_tokens": 60},
        model="gpt-5.5", source="codex", session=row["sessionId"])]))
    assert ship._plan(units, held) == ([], [])


def test_other_sources_and_sessions_do_not_count():
    tid = _trace_id()
    held = ship._shipped(iter([
        _obs(tid, {"input": 100}, oid="x", source="codex"),
        _obs(tid, {"input": 100}, oid="y", session="other")]))
    spans, _ = _spans([_session_row()], shipped=held)
    assert spans[0]["usage"]["input"] == 100


def test_more_in_langfuse_than_ccusage_warns_and_ships_the_rest():
    held = ship._shipped(iter([_obs(_trace_id(), {
        "input": 500, "output": 100})]))
    spans, warnings = _spans([_session_row()], shipped=held)
    assert len(warnings) == 1 and "--reship" in warnings[0]
    assert "input" not in spans[0]["usage"]
    assert spans[0]["usage"]["output"] == 100


def test_daily_units_match_on_date():
    units = ship._units([_daily_row()], HOST, "claude", "daily")
    obs = _obs(units[0]["trace_id"], {"input": 100, "output": 200,
                                      "cache_creation_input_tokens": 300,
                                      "cache_read_input_tokens": 4000})
    obs.pop("sessionId")
    obs["metadata"] = {"source": "claude", "date": "2026-06-10"}
    assert ship._plan(units, ship._shipped(iter([obs]))) == ([], [])


def test_window_starts_at_the_earliest_session_start():
    units = ship._units([_codex_session_row()], HOST, "codex", "session")
    assert ship._window_start(units, "2026-06-01") == "2026-05-27T19:05:55Z"


# --- HTTP ---------------------------------------------------------------------

class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.text = status, body or {}, ""

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ship.requests.HTTPError(str(self.status_code))


def test_ship_posts_otlp_with_the_v4_header(monkeypatch):
    calls = []
    monkeypatch.setattr(ship.requests, "request",
                        lambda m, url, **kw: calls.append((m, url, kw))
                        or _Resp())
    spans, _ = _spans([_session_row()])
    ship._ship("https://lf.example", ("pk", "sk"), spans)
    method, url, kw = calls[0]
    assert (method, url) == ("POST",
                             "https://lf.example/api/public/otel/v1/traces")
    assert kw["headers"] == {"x-langfuse-ingestion-version": "4"}
    assert kw["json"]["resourceSpans"][0]["scopeSpans"][0]["spans"]


def test_ship_raises_on_rejected_spans(monkeypatch):
    monkeypatch.setattr(ship.requests, "request", lambda *a, **k: _Resp(
        body={"partialSuccess": {"rejectedSpans": 1, "errorMessage": "bad"}}))
    with pytest.raises(ship.requests.HTTPError):
        ship._ship("https://lf.example", ("pk", "sk"),
                   _spans([_session_row()])[0])


def test_observations_follow_the_cursor(monkeypatch):
    pages = [_Resp(body={"data": [{"id": "1"}], "meta": {"cursor": "c2"}}),
             _Resp(body={"data": [{"id": "2"}], "meta": {}})]
    seen = []
    monkeypatch.setattr(ship.requests, "request",
                        lambda m, url, **kw: seen.append(dict(kw["params"]))
                        or pages.pop(0))
    got = list(ship._iter_observations("https://lf", ("p", "s"), HOST, "x"))
    assert [o["id"] for o in got] == ["1", "2"]
    assert "cursor" not in seen[0] and seen[1]["cursor"] == "c2"
    assert seen[0]["userId"] == HOST and seen[0]["type"] == "GENERATION"


def test_lock_is_exclusive_and_released(tmp_path):
    a, b = ship._Lock(tmp_path / "l"), ship._Lock(tmp_path / "l")
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_ccusage_subprocess_is_windowless_on_windows():
    # ccusage spawns under pythonw for the silent Scheduled Task; on Windows the
    # subprocess must carry CREATE_NO_WINDOW, and be a harmless 0 elsewhere.
    import sys
    if sys.platform == "win32":
        assert ship._NO_WINDOW == ship.subprocess.CREATE_NO_WINDOW
    else:
        assert ship._NO_WINDOW == 0


def test_heartbeat_pings_url_and_swallows_errors(monkeypatch):
    calls = []

    class _Ok:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(ship.requests, "get",
                        lambda url, timeout=None: calls.append(url) or _Ok())
    ship._ping_heartbeat("https://hc.example/abc")
    assert calls == ["https://hc.example/abc"]

    def _boom(*a, **k):
        raise ship.requests.RequestException("monitor down")

    monkeypatch.setattr(ship.requests, "get", _boom)
    ship._ping_heartbeat("https://hc.example/abc")  # must not raise



# --- --version -----------------------------------------------------------------

def test_version_needs_no_configuration(monkeypatch, capsys):
    for name in ship.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ship, "_ccusage_exe", lambda: "/bin/ccusage")
    monkeypatch.setattr(ship.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": "ccusage 20.0.32\n"})())
    assert ship.main(["--version"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"token-usage {ship.__version__}", "ccusage 20.0.32 (/bin/ccusage)"]


def test_version_reports_a_missing_ccusage(monkeypatch):
    def _missing():
        raise FileNotFoundError("ccusage not found")
    monkeypatch.setattr(ship, "_ccusage_exe", _missing)
    assert ship._versions().endswith("ccusage not found on PATH")


def test_release_stamp_matches_the_source_placeholder():
    """release.yml rewrites exactly this line; if it is reworded, the release
    fails rather than ship a package that calls itself "dev"."""
    src = (Path(__file__).parent / "ccusage-ship.py").read_text()
    assert '\n__version__ = "dev"\n' in src


# --- timestamps in a log file ---------------------------------------------------

def test_log_lines_are_timestamped_once_each():
    import io
    buf = io.StringIO()
    out = ship._Timestamped(buf)
    out.write("[ccusage-ship] a\n[ccusage-ship] b")
    out.write(" continued\n")
    lines = buf.getvalue().splitlines()
    assert len(lines) == 2
    for line in lines:
        stamp, _, rest = line.partition(" [")
        ship.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S%z")
        assert rest.startswith("ccusage-ship]")
    assert lines[1].endswith("b continued")


def test_journald_and_terminals_are_not_stamped(monkeypatch):
    class _Tty:
        def isatty(self):
            return True
    monkeypatch.setattr(ship.sys, "stderr", _Tty())
    ship._stamp_stderr()
    assert not isinstance(ship.sys.stderr, ship._Timestamped)

    class _File:
        def isatty(self):
            return False
    monkeypatch.setenv("JOURNAL_STREAM", "8:12345")
    monkeypatch.setattr(ship.sys, "stderr", _File())
    ship._stamp_stderr()
    assert not isinstance(ship.sys.stderr, ship._Timestamped)

    monkeypatch.delenv("JOURNAL_STREAM")
    ship._stamp_stderr()
    assert isinstance(ship.sys.stderr, ship._Timestamped)

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
