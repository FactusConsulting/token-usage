"""Tests for scripts/ccusage_release_notes.py (no network)."""

import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "notes", pathlib.Path(__file__).with_name("ccusage_release_notes.py"))
notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notes)

# Trimmed from the real v20.0.49 release body.
BODY = """### &nbsp;&nbsp;&nbsp;🚀 Features

- **cline**: Add extension and CLI usage adapter &nbsp;-&nbsp; by @ryoppippi in https://github.com/ccusage/ccusage/issues/1977 [<samp>(35411)</samp>](https://github.com/ccusage/ccusage/commit/35411151)

### &nbsp;&nbsp;&nbsp;🐞 Bug Fixes

- **all**:
  - Count only in-window Claude usage in unified session reports &nbsp;-&nbsp; by @ryoppippi in https://github.com/ccusage/ccusage/issues/2052 [<samp>(5f310)</samp>](https://github.com/ccusage/ccusage/commit/5f31039d)
- **antigravity**:
  - Deduplicate unkeyed mirror usage &nbsp;-&nbsp; by **zhibisora** in https://github.com/ccusage/ccusage/issues/2027 [<samp>(00fea)</samp>](https://github.com/ccusage/ccusage/commit/00fead09)

### &nbsp;&nbsp;&nbsp;🏎 Performance

- Speed up CI and local builds &nbsp;-&nbsp; by @ryoppippi in https://github.com/ccusage/ccusage/issues/1984 [<samp>(4f1f4)</samp>](https://github.com/ccusage/ccusage/commit/4f1f470c)

##### &nbsp;&nbsp;&nbsp;&nbsp;[View changes on GitHub](https://github.com/ccusage/ccusage/compare/v20.0.48...v20.0.49)
"""


def test_parse_inline_nested_and_unscoped_items():
    items = notes.parse(BODY, "20.0.49")
    got = [(i["section"], i["scope"], i["text"], i["number"]) for i in items]
    assert got == [
        ("features", "cline", "Add extension and CLI usage adapter", "1977"),
        ("bug fixes", "all",
         "Count only in-window Claude usage in unified session reports", "2052"),
        ("bug fixes", "antigravity", "Deduplicate unkeyed mirror usage", "2027"),
        ("performance", "", "Speed up CI and local builds", "1984"),
    ]


def test_no_significant_changes_yields_nothing():
    assert notes.parse("*No significant changes*\n\n##### [View](x)", "20.0.48") == []


def test_ranking_and_cap():
    items = notes.parse(BODY, "20.0.49")
    md = notes.render("20.0.48", "20.0.49", items, max_items=2)
    # Feature first, then the fix for "all" ahead of the antigravity fix.
    assert "**cline**: Add extension" in md
    assert "**all**: Count only in-window" in md
    assert "antigravity" not in md and "Speed up" not in md
    assert "…and 2 more upstream changes." in md
    assert "compare/v20.0.48...v20.0.49" in md
    assert "by @" not in md and "samp" not in md and "&nbsp;" not in md


def test_unchanged_version():
    assert "Unchanged at **20.0.49**" in notes.render("20.0.49", "20.0.49", [], 8)
