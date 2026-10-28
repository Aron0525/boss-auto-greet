# ============ 极简 CDP 客户端：绕开 BOSS 的反调试 ============
# 重大发现（2026-10-13 实测）：
#   Playwright 的 connect_over_cdp 会 enable 一批调试域（Runtime/Page/DOM/Log/Network…），
#   BOSS 的前端能据此检测到「调试器已附加」，并在 2~3 秒内把整个页面干掉
#   （现象：TargetClosedError；同一台机器上 example.com 附着后 12 秒+ 毫发无损）。
#   换成裸 websocket 只调 Runtime.evaluate、不 enable 任何域，则页面长期存活（实测 7.5s+ 且持续）。
#
# 因此本模块替代 Playwright，承担「浏览器层」职责：
#   - 只做必须的调用；【绝不】调用 *.enable / *.setAutoAttach / Target.setDiscoverTargets
#   - 读 DOM 用 Runtime.evaluate（返回 JSON）
#   - 点击/输入仍交给 core/human.py 的真实 OS 键鼠（连 CDP 输入事件都不用）
#
# 注意：/json/new 与 Playwright 的 new_page() 创建的标签页会被浏览器还原/丢弃，
#       要新开页请用「启动浏览器进程并带上 URL」的方式（见 scripts/open_boss.py）。

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any

# websocket-client 会读环境代理，本地 CDP 必须绕过
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

import websocket   # noqa: E402


class CdpError(RuntimeError):
    pass


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Cdp:
    """连到一个 page target，只允许「无副作用」的 CDP 调用。"""

    # 明确禁止调用的方法前缀：这些会暴露调试器身份
    FORBIDDEN = (
        "Runtime.enable", "Page.enable", "DOM.enable", "Log.enable",
        "Network.enable", "Debugger.enable", "Target.setDiscoverTargets",
        "Target.setAutoAttach", "Profiler.enable", "Console.enable",
    )

    def __init__(self, base_url: str = "http://127.0.0.1:9222", timeout: float = 20.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self._ws = None
        self._mid = 0
        self.target: dict | None = None
        self._op = _opener()

    # ---------- HTTP 侧：目标管理 ----------

    def list_targets(self) -> list[dict]:
        data = self._op.open(self.base + "/json/list", timeout=self.timeout).read()
        return [t for t in json.loads(data) if t.get("type") == "page"]

    def find_target(self, url_substr: str) -> dict | None:
        for t in self.list_targets():
            if url_substr in (t.get("url") or ""):
                return t
        return None

    def version(self) -> dict:
        data = self._op.open(self.base + "/json/version", timeout=self.timeout).read()
        return json.loads(data)

    # ---------- ws 侧 ----------

    def attach(self, target: dict) -> "Cdp":
        """附着到指定 target。target 取自 list_targets()/find_target()。"""
        self.close()
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise CdpError("该 target 没有 webSocketDebuggerUrl")
        self._ws = websocket.create_connection(ws_url, timeout=self.timeout,
                                               suppress_origin=True)
        self._mid = 0
        self.target = target
        return self

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def call(self, method: str, params: dict | None = None,
             timeout: float | None = None) -> dict:
        """发一条 CDP 命令并等回复。禁止调用 FORBIDDEN 里的方法。"""
        if method in self.FORBIDDEN or method.split(".")[0] + ".enable" == method:
            raise CdpError(f"禁止调用 {method}（会暴露调试器身份，导致页面被杀）")
        if self._ws is None:
            raise CdpError("尚未 attach")
        if timeout:
            self._ws.settimeout(timeout)

        self._mid += 1
        mid = self._mid
        self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))

        # 跳过事件通知（没有 id 的消息），直到拿到本次回复
        deadline = time.time() + (timeout or self.timeout)
        while True:
            if time.time() > deadline:
                raise CdpError(f"{method} 超时")
            raw = self._ws.recv()
            if not raw:
                raise CdpError("连接已关闭")
            msg = json.loads(raw)
            if msg.get("id") != mid:
                continue
            if "error" in msg:
                raise CdpError(f"{method} 失败: {msg['error']}")
            return msg.get("result") or {}

    # ---------- 常用能力 ----------

    def evaluate(self, expression: str, await_promise: bool = True) -> Any:
        """在页面里执行 JS，返回 JSON 化的值。"""
        r = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        if r.get("exceptionDetails"):
            desc = (r["exceptionDetails"].get("exception") or {}).get("description")
            raise CdpError(f"JS 异常: {desc or r['exceptionDetails'].get('text')}")
        return (r.get("result") or {}).get("value")

    def evaluate_json(self, fn_body: str) -> Any:
        """把一段「返回对象的函数体」包成 IIFE 执行并取回结果。"""
        return self.evaluate("(() => {" + fn_body + "})()")

    def bring_to_front(self):
        try:
            self.call("Page.bringToFront")
        except Exception:
            pass

    def screenshot(self, path: str, full_page: bool = False) -> bool:
        """截图（Page.captureScreenshot 不要求 Page.enable）。"""
        import base64
        params = {"format": "png"}
        if full_page:
            params["captureBeyondViewport"] = True
        try:
            r = self.call("Page.captureScreenshot", params, timeout=30)
        except Exception:
            return False
        data = r.get("data")
        if not data:
            return False
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        return True

    def url(self) -> str:
        try:
            return self.evaluate("location.href") or ""
        except Exception:
            return ""

    def title(self) -> str:
        try:
            return self.evaluate("document.title") or ""
        except Exception:
            return ""

    def ready_state(self) -> str:
        try:
            return self.evaluate("document.readyState") or ""
        except Exception:
            return ""

    def close(self):
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        self._ws = None
        self.target = None

    # 上下文管理器
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False
