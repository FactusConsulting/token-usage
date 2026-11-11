"""Tests for scripts/ccusage_pin.py (no network)."""

import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "pin", pathlib.Path(__file__).with_name("ccusage_pin.py"))
pin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pin)


def test_repo_pin_files_agree():
    """CCUSAGE_VERSION and ccusage.lock.json must name the same version and
    cover every platform a package channel needs. Guards a hand edit to one
    file without the other."""
    assert pin.check() == []


def _fake_registry(version):
    def fetch(url):
        name = url.split("/")[-2].replace("%2F", "/")
        plat = name.rsplit("ccusage-", 1)[1]
        return {"dist": {
            "tarball": f"https://registry.npmjs.org/{name}/-/ccusage-{plat}-{version}.tgz",
            "integrity": f"sha512-{plat}=="}}
    return fetch


def test_build_lock_takes_platforms_from_launcher():
    launcher = {"optionalDependencies": {
        "@ccusage/ccusage-linux-x64": "1.2.3",
        "@ccusage/ccusage-win32-x64": "1.2.3",
        "unrelated": "9.9.9",
    }}
    lock = pin.build_lock("1.2.3", launcher, fetch=_fake_registry("1.2.3"))
    assert lock["version"] == "1.2.3"
    assert sorted(lock["platforms"]) == ["linux-x64", "win32-x64"]
    assert lock["platforms"]["linux-x64"]["integrity"] == "sha512-linux-x64=="


def test_build_lock_rejects_mismatched_platform_version():
    launcher = {"optionalDependencies": {"@ccusage/ccusage-linux-x64": "1.2.2"}}
    with pytest.raises(ValueError, match="expected 1.2.3"):
        pin.build_lock("1.2.3", launcher, fetch=_fake_registry("1.2.3"))
