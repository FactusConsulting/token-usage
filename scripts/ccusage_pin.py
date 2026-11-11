#!/usr/bin/env python3
"""Maintain the ccusage pin: CCUSAGE_VERSION and ccusage.lock.json.

    ccusage_pin.py update [--version X]  pin the latest (or X) ccusage
    ccusage_pin.py check                 the two files agree (CI, offline)
    ccusage_pin.py verify                download every pinned binary, check
                                         its integrity, run the one for this
                                         host and compare its --version

ccusage 20.x ships a static native binary per platform as the npm package
`@ccusage/ccusage-<platform>`. The `ccusage` package itself is only a Node
launcher that picks one. ccusage.lock.json records the tarball URL and npm's
sha512 integrity for each platform at the pinned version, so a package can
fetch the binary itself, without Node, and verify it. The Nix flake uses the
integrity string directly as a fixed-output hash.

`update` writes GitHub Actions outputs (changed, version) when GITHUB_OUTPUT
is set. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import pathlib
import platform
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "CCUSAGE_VERSION"
LOCK_FILE = ROOT / "ccusage.lock.json"
REGISTRY = "https://registry.npmjs.org"
PREFIX = "@ccusage/ccusage-"


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def _get_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


def build_lock(version: str, launcher: dict, fetch=_get_json) -> dict:
    """Lock data for `version`, from the launcher package's metadata.

    The platform list comes from the launcher's optionalDependencies, so a
    platform upstream adds or drops shows up here without a code change.
    """
    platforms = {}
    for name, dep_version in sorted(launcher.get("optionalDependencies", {}).items()):
        if not name.startswith(PREFIX):
            continue
        if dep_version != version:
            raise ValueError(f"{name} is pinned at {dep_version}, expected {version}")
        meta = fetch(f"{REGISTRY}/{name.replace('/', '%2F')}/{version}")
        dist = meta["dist"]
        if not dist.get("integrity", "").startswith("sha512-"):
            raise ValueError(f"{name}@{version} has no sha512 integrity")
        platforms[name[len(PREFIX):]] = {
            "url": dist["tarball"],
            "integrity": dist["integrity"],
        }
    if not platforms:
        raise ValueError(f"ccusage@{version} lists no {PREFIX}* binaries")
    return {"version": version, "platforms": platforms}


def read_pin() -> tuple[str, dict | None]:
    version = VERSION_FILE.read_text().strip()
    lock = json.loads(LOCK_FILE.read_text()) if LOCK_FILE.exists() else None
    return version, lock


def check() -> list[str]:
    """Problems with the pin files; empty when they agree."""
    version, lock = read_pin()
    if lock is None:
        return [f"{LOCK_FILE.name} is missing"]
    problems = []
    if lock.get("version") != version:
        problems.append(f"{LOCK_FILE.name} pins {lock.get('version')}, "
                        f"CCUSAGE_VERSION pins {version}")
    for plat, entry in lock.get("platforms", {}).items():
        if f"-{version}.tgz" not in entry.get("url", ""):
            problems.append(f"{plat}: URL is not for {version}: {entry.get('url')}")
        if not entry.get("integrity", "").startswith("sha512-"):
            problems.append(f"{plat}: integrity is not sha512")
    for required in ("linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64",
                     "win32-x64"):
        if required not in lock.get("platforms", {}):
            problems.append(f"{required}: missing; a package channel needs it")
    return problems


def _integrity(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def _host_platform() -> str | None:
    os_name = {"Linux": "linux", "Darwin": "darwin", "Windows": "win32"}.get(platform.system())
    arch = {"x86_64": "x64", "AMD64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    return f"{os_name}-{arch}" if os_name and arch else None


def verify() -> None:
    """Download every pinned binary and check it against the lock."""
    version, lock = read_pin()
    host = _host_platform()
    for plat, entry in sorted(lock["platforms"].items()):
        data = _get_bytes(entry["url"])
        if _integrity(data) != entry["integrity"]:
            raise SystemExit(f"{plat}: integrity mismatch for {entry['url']}")
        print(f"{plat}: integrity ok ({len(data)} bytes)")
        if plat != host:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                tar.extractall(tmp, filter="data")
            exe = pathlib.Path(tmp, "package", "bin",
                               "ccusage.exe" if plat.startswith("win32") else "ccusage")
            exe.chmod(0o755)
            out = subprocess.run([str(exe), "--version"], capture_output=True,
                                 text=True, timeout=60, check=True).stdout
            got = (out.split() or ["?"])[-1]
            if got != version:
                raise SystemExit(f"{plat}: binary reports {got}, pinned {version}")
            print(f"{plat}: runs, reports ccusage {got}")


def update(version: str | None) -> tuple[bool, str]:
    launcher = _get_json(f"{REGISTRY}/ccusage/{version or 'latest'}")
    new = launcher["version"]
    current, lock = read_pin()
    if new == current and lock and lock.get("version") == new and not check():
        return False, new
    lock = build_lock(new, launcher)
    VERSION_FILE.write_text(new + "\n")
    LOCK_FILE.write_text(json.dumps(lock, indent=2) + "\n")
    return True, new


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("update")
    up.add_argument("--version", help="pin this version instead of npm's latest")
    sub.add_parser("check")
    sub.add_parser("verify")
    args = ap.parse_args(argv)

    if args.cmd == "check":
        problems = check()
        for p in problems:
            print(f"ccusage pin: {p}", file=sys.stderr)
        return 1 if problems else 0
    if args.cmd == "verify":
        verify()
        return 0

    old = VERSION_FILE.read_text().strip()
    changed, new = update(args.version)
    print(f"ccusage pin: {old} -> {new}" if changed else f"ccusage pin: {new} is current")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"changed={'true' if changed else 'false'}\nversion={new}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
