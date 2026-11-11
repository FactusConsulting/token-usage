#!/usr/bin/env python3
"""Summarise upstream ccusage release notes for a token-usage release.

    ccusage_release_notes.py --from 20.0.45 --to 20.0.49 [--max-items 8]

Fetches every ccusage GitHub release in the range (from, to], pulls the
Features / Bug Fixes / Performance items out of each and prints a short
Markdown section with the most important ones. Features rank above fixes,
which rank above performance work, and items for the sources we ship
(claude, codex, ...) or for all sources rank above other adapters.

This runs inside the release job, so it never fails the release. If GitHub
cannot be reached, the output is just the version change and a compare link.
Standard library only: the release runner has python3 and nothing else.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

UPSTREAM = "ccusage/ccusage"

# Section heading -> (rank, label). Lower rank is more important.
SECTIONS = {
    "features": (0, "🚀 Features"),
    "bug fixes": (1, "🐞 Bug Fixes"),
    "performance": (2, "🏎 Performance"),
}

# Scopes that change what the shim ships. "all", "adapter" and "" (no scope)
# apply to every source.
RELEVANT_SCOPES = {"", "all", "adapter", "claude", "codex", "pi", "copilot",
                   "gemini", "opencode"}


def vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v)[:3])


def clean(text: str) -> str:
    """Strip changelogithub's "&nbsp;-&nbsp; by @x in <url> [(sha)](url)" tail."""
    text = text.replace("&nbsp;", " ")
    text = re.split(r"\s+-\s+by\s+", text, maxsplit=1)[0]
    text = re.sub(r"\[<samp>.*?</samp>\]\(.*?\)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse(body: str, version: str) -> list[dict]:
    """Return the items in one upstream release body.

    changelogithub writes either `- **scope**: text` or, when one scope has
    several items, `- **scope**:` followed by indented `  - text` lines.
    """
    items: list[dict] = []
    section = None
    scope = ""
    for raw in body.splitlines():
        line = raw.replace("&nbsp;", " ").rstrip()
        heading = re.match(r"^#{2,4}\s+(.*)$", line)
        if heading:
            title = re.sub(r"[^a-z ]", "", heading.group(1).lower()).strip()
            section = title if title in SECTIONS else None
            continue
        if section is None:
            continue
        top = re.match(r"^- (?:\*\*(?P<scope>[^*]+)\*\*:)?\s*(?P<text>.*)$", line)
        nested = re.match(r"^\s{2,}- (?P<text>.*)$", line)
        if nested:
            text = nested.group("text")
        elif top:
            scope = (top.group("scope") or "").strip().lower()
            text = top.group("text")
            if not text:  # `- **scope**:` header for nested items
                continue
        else:
            continue
        issue = re.search(r"https://github\.com/\S+?/issues/(\d+)", text)
        items.append({
            "section": section,
            "scope": scope,
            "text": clean(text),
            "issue": issue.group(0) if issue else None,
            "number": issue.group(1) if issue else None,
            "version": version,
        })
    return items


def rank(item: dict) -> tuple:
    return (
        SECTIONS[item["section"]][0],
        0 if item["scope"] in RELEVANT_SCOPES else 1,
        tuple(-p for p in vtuple(item["version"])),  # newest first
    )


def fetch_releases(token: str | None) -> list[dict]:
    releases: list[dict] = []
    for page in range(1, 4):  # 300 releases is far more than one gap needs
        req = urllib.request.Request(
            f"https://api.github.com/repos/{UPSTREAM}/releases"
            f"?per_page=100&page={page}",
            headers={"Accept": "application/vnd.github+json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            batch = json.load(resp)
        releases += batch
        if len(batch) < 100:
            break
    return releases


def render(old: str, new: str, items: list[dict], max_items: int) -> str:
    compare = f"https://github.com/{UPSTREAM}/compare/v{old}...v{new}"
    if vtuple(old) == vtuple(new):
        return f"## ccusage\n\nUnchanged at **{new}**.\n"
    out = [f"## ccusage {old} → {new}", ""]
    if not items:
        out += ["No upstream release notes could be summarised.", ""]
    else:
        top = sorted(items, key=rank)[:max_items]
        for key, (_, label) in sorted(SECTIONS.items(), key=lambda kv: kv[1][0]):
            picked = [i for i in top if i["section"] == key]
            if not picked:
                continue
            out += [f"### {label}", ""]
            for i in picked:
                scope = f"**{i['scope']}**: " if i["scope"] else ""
                ref = f" ([#{i['number']}]({i['issue']}))" if i["issue"] else ""
                out.append(f"- {scope}{i['text']}{ref} — {i['version']}")
            out.append("")
        if len(items) > len(top):
            out += [f"…and {len(items) - len(top)} more upstream changes.", ""]
    out.append(f"Full upstream changelog: {compare}")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from", dest="old", required=True)
    ap.add_argument("--to", dest="new", required=True)
    ap.add_argument("--max-items", type=int, default=8)
    args = ap.parse_args(argv)

    items: list[dict] = []
    if vtuple(args.old) < vtuple(args.new):
        try:
            releases = fetch_releases(os.environ.get("GITHUB_TOKEN")
                                      or os.environ.get("GH_TOKEN"))
        except Exception as exc:  # never fail the release over notes
            print(f"::warning::could not fetch ccusage releases: {exc}",
                  file=sys.stderr)
            releases = []
        for rel in releases:
            v = rel.get("tag_name", "").lstrip("v")
            if rel.get("draft") or rel.get("prerelease") or not v:
                continue
            if vtuple(args.old) < vtuple(v) <= vtuple(args.new):
                items += parse(rel.get("body") or "", v)
    sys.stdout.write(render(args.old, args.new, items, args.max_items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
