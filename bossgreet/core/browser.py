# ============ 基础设施层：连接已登录浏览器 + 随机延时 ============
# 重要变更（2026-10-13）：
#   原实现用 Playwright 的 connect_over_cdp 读写页面。实测 BOSS 前端能检测到
#   Playwright 开启的调试域，并在 2~3 秒内把页面干掉（TargetClosedError）。
#   改为【裸 CDP】（core/cdp.py，只调 Runtime.evaluate、不 enable 任何域）后
#   页面长期稳定存活。因此本模块不再依赖 Playwright。

from __future__ import annotations

import random
import time

from bossgreet.core.cdp import Cdp, CdpError


def human_delay(min_s: float = 8, max_s: float = 20):
    """模拟真人节奏：随机停顿。"""
    time.sleep(random.uniform(min_s, max_s))


class BrowserSession:
    """附着到「已登录浏览器里那个 BOSS 页」，复用登录态（不碰密码）。

    退出时只关闭 CDP 连接，不动浏览器、不动调试端口、不清登录态。
    """

    def __init__(self, cdp_url: str):
        self.cdp_url = cdp_url
        self.cdp = Cdp(cdp_url)
        self._browser_name = ""

    # ---------- 连接 ----------

    def connect(self, url_substr: str = "/web/geek/jobs") -> Cdp:
        """只附着 BOSS 职位页，避免在登录或安全校验页继续自动化。"""
        t = None
        for _ in range(20):
            t = self.cdp.find_target(url_substr)
            if t is not None:
                break
            time.sleep(0.5)
        if t is None:
            raise RuntimeError(
                "配置的浏览器里没有 BOSS 职位页。请先在专用窗口完成登录，"
                "再重新运行相同命令。")
        self.cdp.attach(t)
        self._browser_name = self._detect_browser_name()
        return self.cdp

    def _detect_browser_name(self) -> str:
        try:
            ua = self.cdp.evaluate("navigator.userAgent") or ""
        except Exception:
            ua = ""
        if "Edg/" in ua:
            return "Microsoft Edge"
        if "Chrome/" in ua:
            return "Google Chrome"
        return "未知浏览器"

    def browser_name(self) -> str:
        return self._browser_name or "未知浏览器"

    @property
    def page(self) -> Cdp:
        return self.cdp

    # ---------- 目标管理 ----------

    def all_targets(self) -> list[dict]:
        try:
            return self.cdp.list_targets()
        except Exception:
            return []

    def disconnect(self):
        """只断开 CDP 连接，绝不关闭浏览器。"""
        self.cdp.close()

    close = disconnect


def jitter_sleep(min_s: float = 0.4, max_s: float = 1.2):
    time.sleep(random.uniform(min_s, max_s))
