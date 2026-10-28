"""浏览器选择和启动测试；使用模拟端口与进程，不启动真实浏览器。"""
from types import SimpleNamespace

import pytest

from bossgreet.core import launcher


def _config(name="edge", **overrides):
    browser = {"name": name, "cdp_url": "http://127.0.0.1:9222", **overrides}
    return {"browser": browser}


def test_reuse_only_matching_configured_browser(monkeypatch):
    monkeypatch.setattr(launcher, "_probe", lambda _: {"Browser": "Edg/123"})
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *_a, **_k: pytest.fail("不应新启动"))
    assert launcher.ensure_browser(_config("edge")) is False
    with pytest.raises(RuntimeError, match="其他浏览器"):
        launcher.ensure_browser(_config("chrome"))


def test_start_chosen_browser_with_dedicated_local_profile(monkeypatch, tmp_path):
    versions = iter([None, {"Browser": "Chrome/123"}])
    monkeypatch.setattr(launcher, "_probe", lambda _: next(versions))
    monkeypatch.setattr(launcher, "_executable", lambda *_: "chrome.exe")
    monkeypatch.setattr(launcher, "_profile_dir", lambda *_: tmp_path / "profile")
    monkeypatch.setattr(launcher.time, "sleep", lambda *_: None)
    launched = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda argv, **_kw: (
        launched.append(argv) or SimpleNamespace(poll=lambda: None)))

    assert launcher.ensure_browser(_config("chrome")) is True
    assert launched[0][0] == "chrome.exe"
    assert "--remote-debugging-port=9222" in launched[0]
    assert f"--user-data-dir={tmp_path / 'profile'}" in launched[0]
    assert launcher.LOGIN_URL in launched[0]
    assert (tmp_path / "profile").is_dir()


def test_open_jobs_page_only_when_it_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "_probe", lambda _: {"Browser": "Edg/123"})
    monkeypatch.setattr(launcher, "_executable", lambda *_: "msedge.exe")
    monkeypatch.setattr(launcher, "_profile_dir", lambda *_: tmp_path / "profile")
    launched = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda argv, **_kw: launched.append(argv))

    monkeypatch.setattr(launcher, "_has_jobs_page", lambda _: True)
    assert launcher.ensure_jobs_page(_config()) is False
    assert launched == []

    monkeypatch.setattr(launcher, "_has_jobs_page", lambda _: False)
    assert launcher.ensure_jobs_page(_config()) is True
    assert launcher.JOBS_URL in launched[0]
    assert f"--user-data-dir={tmp_path / 'profile'}" in launched[0]


def test_disabled_autostart_and_nonlocal_cdp_stop(monkeypatch):
    monkeypatch.setattr(launcher, "_probe", lambda _: None)
    with pytest.raises(RuntimeError, match="auto_start"):
        launcher.ensure_browser(_config(auto_start=False))
    with pytest.raises(ValueError, match="本机 HTTP"):
        launcher.ensure_browser({"browser": {"name": "edge", "cdp_url": "http://example.invalid:9222"}})
