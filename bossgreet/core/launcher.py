"""按本机配置选择并启动专用浏览器，不读取账号或登录凭据。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

# 首次启动只到登录页：账号、密码、扫码和安全校验必须由使用者完成。登录后，
# CLI 才会按需通过同一专用 profile 打开职位页。
LOGIN_URL = "https://www.zhipin.com/web/user/?ka=header-login"
JOBS_URL = "https://www.zhipin.com/web/geek/jobs"
_NAMES = {"edge": "msedge.exe", "chrome": "chrome.exe"}


def _settings(cfg: dict) -> tuple[dict, str, str, int]:
    browser = cfg.get("browser") or {}
    name = str(browser.get("name") or "edge").lower()
    if name not in _NAMES:
        raise ValueError("browser.name 只支持 edge 或 chrome")
    base = str(browser.get("cdp_url") or "http://127.0.0.1:9222").rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.path:
        raise ValueError("browser.cdp_url 必须是本机 HTTP 地址，例如 http://127.0.0.1:9222")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("browser.cdp_url 的端口不合法") from exc
    if port is None:
        raise ValueError("browser.cdp_url 必须包含端口")
    return browser, name, base, port


def _probe(base: str) -> dict | None:
    try:
        with urlopen(base + "/json/version", timeout=1.5) as response:
            return json.load(response)
    except (URLError, OSError, ValueError):
        return None


def _has_jobs_page(base: str) -> bool:
    """仅查看本机 CDP 的标签页列表，不附着页面也不读取登录信息。"""
    try:
        with urlopen(base + "/json/list", timeout=1.5) as response:
            targets = json.load(response)
    except (URLError, OSError, ValueError):
        return False
    return any(
        target.get("type") == "page" and "/web/geek/jobs" in (target.get("url") or "")
        for target in targets
    )


def _engine_from_version(version: dict) -> str:
    details = f"{version.get('Browser', '')} {version.get('User-Agent', '')}"
    if "Edg/" in details:
        return "edge"
    if "Chrome/" in details:
        return "chrome"
    return ""


def _executable(browser: dict, name: str) -> str:
    configured = str(browser.get("executable") or "").strip()
    if configured:
        path = Path(os.path.expandvars(configured)).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"browser.executable 指向的文件不存在：{path}")
        return str(path.resolve())

    found = shutil.which(_NAMES[name])
    if found:
        return found
    vendor = Path("Microsoft/Edge/Application/msedge.exe" if name == "edge"
                  else "Google/Chrome/Application/chrome.exe")
    for env_key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(env_key)
        if root:
            path = Path(root) / vendor
            if path.is_file():
                return str(path)
    raise FileNotFoundError(
        f"找不到 {name} 浏览器；请在 config.yaml 设置 browser.executable 的完整路径")


def _profile_dir(browser: dict, name: str) -> Path:
    raw = str(browser.get("user_data_dir") or "").strip()
    if raw:
        path = Path(os.path.expandvars(raw)).expanduser()
        return path if path.is_absolute() else Path.cwd() / path
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local / "bossgreet" / f"{name}-profile"


def ensure_browser(cfg: dict) -> bool:
    """确保所选浏览器在配置端口运行；新启动时返回 True。"""
    if sys.platform != "win32":
        raise RuntimeError("浏览器自动启动目前仅支持 Windows")
    browser, name, base, port = _settings(cfg)
    version = _probe(base)
    if version is not None:
        actual = _engine_from_version(version)
        if actual != name:
            raise RuntimeError(f"端口 {port} 已被其他浏览器占用；配置选择的是 {name}")
        return False

    if not browser.get("auto_start", True):
        raise RuntimeError("CDP 端口未启动且 browser.auto_start 为 false")

    executable = _executable(browser, name)
    profile = _profile_dir(browser, name)
    profile.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [executable, f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}", LOGIN_URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"[*] 已启动配置的 {name} 浏览器并打开 BOSS 登录页；请在专用窗口手动登录。")
    for _ in range(60):
        time.sleep(0.25)
        version = _probe(base)
        if version is not None:
            if _engine_from_version(version) != name:
                raise RuntimeError(f"端口 {port} 对应的浏览器与 browser.name 不一致")
            return True
        if process.poll() is not None:
            break
    raise RuntimeError("浏览器已启动但 CDP 端口未就绪；请关闭同一专用配置目录的旧进程后重试")


def ensure_jobs_page(cfg: dict) -> bool:
    """登录后的预览/运行请求按需打开职位页；已有职位页时绝不重复开标签。"""
    if sys.platform != "win32":
        raise RuntimeError("浏览器自动启动目前仅支持 Windows")
    browser, name, base, port = _settings(cfg)
    version = _probe(base)
    if version is None:
        raise RuntimeError("浏览器调试端口未启动")
    actual = _engine_from_version(version)
    if actual != name:
        raise RuntimeError(f"端口 {port} 已被其他浏览器占用；配置选择的是 {name}")
    if _has_jobs_page(base):
        return False

    executable = _executable(browser, name)
    profile = _profile_dir(browser, name)
    profile.mkdir(parents=True, exist_ok=True)
    # Chromium 会把同 profile 的启动请求转交给已运行实例，因此不会创建另一份登录态。
    subprocess.Popen(
        [executable, f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}", JOBS_URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print("[*] 已在同一专用浏览器中打开 BOSS 职位页。")
    return True
