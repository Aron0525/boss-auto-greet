# ============ 把浏览器窗口挪出屏幕：全程不占屏幕、不抢鼠标 ============
# 为什么需要它（2026-10-18 实测定论）：
#   · 输入早就是 CDP 派发（core/cdpinput.py），**不动真实光标**；
#   · 但窗口自己会跑到最前面 —— `--new-window` 启动时 Edge 会激活自己，
#     窗口就盖在你的工作区上，鼠标点哪儿都先打到它身上。用户的原话是
#     「程序运行不能出现在前端，不能占据我鼠标的使用权」。
#   · 解法不是"抢回来"，而是让它压根不在屏幕上：
#       Browser.getWindowForTarget → Browser.setWindowBounds(left=-32000)
#     窗口移到屏幕外后页面会被标成 hidden，这不影响 CDP —— 点击前过一遍
#     `ensure_interactive()`（setWebLifecycleState + 焦点模拟）就能恢复可点。
#
# 实测（scripts/_probe_park.py，2026-10-18）：
#   · x=-32000 时 window.screenX=-32000、visibility 由 hidden 恢复 visible；
#   · 坐标点击有效（详情面板标题从「硬件工程师」切到「充电桩硬件工程师」）；
#   · Page.navigate 有效（15 张卡片正常加载）；
#   · 前台窗口始终是用户自己的窗口，Edge 一次都没跳到前面。
#
# 注意：Browser.* 必须挂在 /json/version 给的 **browser 级 ws** 上，
#       page 级 ws 调用会报 "not found"。这条连接是真·浏览器级，不碰页面，
#       不会触发 BOSS 的反调试（那里禁的是 *.enable / Target.setAutoAttach）。
from __future__ import annotations

import ctypes
import json
import sys
import threading
import urllib.request

import websocket

# 本模块里【窗口/焦点】那一半用的是 Win32 API（ctypes.windll），只有 Windows 有。
# 但「把窗口挪到屏幕外」本身走的是 CDP 的 Browser.setWindowBounds，跨平台可用。
# 所以这里只让 ctypes 相关的能力在非 Windows 上安全降级，而不是整个模块失效 ——
# 这样 Linux/macOS 上照样能连浏览器跑采集、预览、投递。
IS_WINDOWS = sys.platform == "win32"

# 屏幕外的"停车位"。用 -32000 是因为 Chromium 自己也用这个值表示"窗口在屏幕外"，
# 系统不会把它当成异常，DWM 也不会把它算进任务栏预览。
PARK_X = -32000
PARK_Y = 0


def _user32():
    """拿 user32；非 Windows 抛 RuntimeError（调用方自行降级）。"""
    if not IS_WINDOWS:
        raise RuntimeError("窗口焦点 API 仅 Windows 可用")
    return ctypes.windll.user32


def _http_json(base: str, path: str, timeout: float = 5.0) -> dict:
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return json.loads(op.open(base.rstrip("/") + path, timeout=timeout).read())


def browser_call(base: str, method: str, params: dict | None = None,
                 timeout: float = 8.0) -> dict:
    """在 browser 级 ws 上发一条 CDP 调用。"""
    ws_url = _http_json(base, "/json/version")["webSocketDebuggerUrl"]
    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(f"{method} 失败：{msg['error']}")
                return msg.get("result") or {}
    finally:
        ws.close()


def window_bounds(base: str, target_id: str) -> tuple[int, dict]:
    """返回 (windowId, bounds)。base 形如 http://127.0.0.1:9222。"""
    r = browser_call(base, "Browser.getWindowForTarget", {"targetId": target_id})
    return int(r.get("windowId")), (r.get("bounds") or {})


def set_bounds(base: str, window_id: int, left: int, top: int,
               width: int | None = None, height: int | None = None) -> None:
    bounds: dict = {"left": int(left), "top": int(top), "windowState": "normal"}
    if width:
        bounds["width"] = int(width)
    if height:
        bounds["height"] = int(height)
    browser_call(base, "Browser.setWindowBounds",
                 {"windowId": window_id, "bounds": bounds})


def is_offscreen(bounds: dict) -> bool:
    return int(bounds.get("left", 0)) <= -30000 or int(bounds.get("top", 0)) <= -30000


def park(cdp, x: int = PARK_X, y: int = PARK_Y) -> bool:
    """把 cdp 所在页面的窗口挪到屏幕外。成功返回 True。"""
    try:
        wid, b = window_bounds(cdp.base, cdp.target["id"])
        set_bounds(cdp.base, wid, x, y,
                   b.get("width") or None, b.get("height") or None)
        return True
    except Exception as e:
        print(f"     [!] 挪窗口失败（不影响点击，只是窗口会留在屏幕上）：{e}")
        return False


def park_status(cdp) -> dict:
    """当前窗口位置。{left, top, offscreen} —— 读不到返回 {}。"""
    try:
        _, b = window_bounds(cdp.base, cdp.target["id"])
        return {"left": b.get("left"), "top": b.get("top"),
                "width": b.get("width"), "height": b.get("height"),
                "offscreen": is_offscreen(b)}
    except Exception:
        return {}


def show(cdp, x: int = 80, y: int = 60) -> bool:
    """把窗口搬回屏幕上（用户想自己看一眼 / 登录时用）。"""
    try:
        wid, b = window_bounds(cdp.base, cdp.target["id"])
        set_bounds(cdp.base, wid, x, y,
                   b.get("width") or None, b.get("height") or None)
        # 手动调用才允许抢前台（这是用户主动要求"给我看看"）
        try:
            cdp.call("Page.bringToFront")
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"     [!] 搬回窗口失败：{e}")
        return False


# ---------- 只读探针：当前前台窗口是谁 ----------

def foreground_title() -> str:
    """当前前台窗口标题（纯读取，不动任何东西）。"""
    try:
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 2)
        u.GetWindowTextW(h, buf, n + 2)
        return buf.value
    except Exception:
        return ""


def fg_hwnd() -> int:
    try:
        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return 0


def window_title(hwnd: int) -> str:
    try:
        u = ctypes.windll.user32
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 2)
        u.GetWindowTextW(hwnd, buf, n + 2)
        return buf.value
    except Exception:
        return ""


def restore_foreground(hwnd: int) -> bool:
    """把前台还给 hwnd 那个窗口。返回是否真的成功了（以 hwnd 为准校验）。

    为什么需要：Chromium 启动窗口时会【自己】调 SetForegroundWindow，
    STARTUPINFO 的 SW_SHOWNOACTIVATE / SW_SHOWMINNOACTIVE 它都无视
    （2026-10-18 三种启动姿势实测）。于是窗口明明在屏幕外，键盘焦点却被
    它拿走了（你打字会打到一个看不见的窗口里）。
    这里依次尝试三种绕过方式（都不移动鼠标、不点任何东西）：
      1. AttachThreadInput 跨线程附加 + SetForegroundWindow
      2. 模拟一次 Alt 键解锁前台锁 + SetForegroundWindow
      3. SwitchToThisWindow（未公开 API）
    """
    if not hwnd:
        return False
    try:
        import ctypes.wintypes as wt
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        if not u.IsWindow(hwnd):
            return False
        if u.GetForegroundWindow() == hwnd:
            return True

        def _attach_set() -> None:
            cur = k.GetCurrentThreadId()
            fg = u.GetForegroundWindow()
            tids = []
            for h in (fg, hwnd):
                if h:
                    tid = u.GetWindowThreadProcessId(h, None)
                    if tid and tid != cur and u.AttachThreadInput(cur, tid, True):
                        tids.append(tid)
            try:
                u.SetForegroundWindow(hwnd)
            finally:
                for tid in tids:
                    u.AttachThreadInput(cur, tid, False)

        _attach_set()
        if u.GetForegroundWindow() == hwnd:
            return True

        VK_MENU, KEYUP = 0x12, 0x0002
        u.keybd_event(VK_MENU, 0, 0, 0)          # 只按一下 Alt 再松开，
        u.keybd_event(VK_MENU, 0, KEYUP, 0)      # 不输入任何字符
        _attach_set()
        if u.GetForegroundWindow() == hwnd:
            return True

        try:
            u.SwitchToThisWindow(hwnd, True)
        except Exception:
            pass
        return u.GetForegroundWindow() == hwnd
    except Exception:
        return False


def looks_like_browser(title: str) -> bool:
    """按标题猜是不是浏览器窗口。

    ⚠ 别只靠它：Edge 的标题里有零宽空格（'Microsoft\\u200b Edge'），
    字符串匹配会漏。判断「前台是不是浏览器」请用 is_browser_foreground()，
    它看的是窗口类名（Edge/Chrome 都是 Chrome_WidgetWin_1）。
    """
    t = (title or "").lower()
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        t = t.replace(ch, "")
    return ("microsoft edge" in t) or ("google chrome" in t) or ("msedge" in t)


def window_class(hwnd: int) -> str:
    try:
        u = ctypes.windll.user32
        buf = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, buf, 256)
        return buf.value
    except Exception:
        return ""


def process_name(hwnd: int) -> str:
    """窗口所属进程的可执行文件名（小写），读不到返回 ""。"""
    try:
        import ctypes.wintypes as wt
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        h = k.OpenProcess(0x1000, False, pid.value)      # QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            size = wt.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if not k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return ""
            return buf.value.replace("\\", "/").split("/")[-1].lower()
        finally:
            k.CloseHandle(h)
    except Exception:
        return ""


BROWSER_PROCS = ("msedge.exe", "chrome.exe")


def is_browser_foreground() -> bool:
    """前台是不是 Edge/Chrome 本体。

    ⚠ 不能按窗口类名判断：WorkBuddy 自己也是 Electron，类名同样是
    Chrome_WidgetWin_1（2026-10-18 实测把探针坑了）。只能看进程名。
    """
    try:
        return process_name(ctypes.windll.user32.GetForegroundWindow()) in BROWSER_PROCS
    except Exception:
        return False


def screen_size() -> tuple[int, int]:
    try:
        u = ctypes.windll.user32
        return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    except Exception:
        return (0, 0)


def window_rect(hwnd: int):
    """(left, top, right, bottom)，读不到返回 None。"""
    try:
        import ctypes.wintypes as wt
        r = wt.RECT()
        if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return (r.left, r.top, r.right, r.bottom)
        return None
    except Exception:
        return None


class FocusGuard(threading.Thread):
    """常驻守卫：只要「被我挪到屏幕外的那类窗口」抢到前台，立刻把焦点还回去。

    为什么用线程而不是启动时补一次：
      Chromium 抢焦点的时机不固定（构造窗口 / 首次渲染 / 页面加载完成 / 切标签页
      都可能触发），2026-10-18 三次实测两次抢、一次不抢。放过一次就够你难受半天。
    怎么认定"那是我们的窗口"：
      屏幕外的窗口横坐标 <= -30000（只有被 park() 挪走的窗口才这样），
      用户自己的浏览器窗口在屏幕上，永远不会被误判 —— 所以不会跟用户抢。
    """

    def __init__(self, interval: float = 0.8):
        super().__init__(daemon=True)
        self.interval = max(0.2, interval)
        self.fixes = 0
        self.fails = 0
        self.last_user_hwnd = 0
        self._stop = threading.Event()
        # 非 Windows 上拿不到 user32 —— 不要在这里抛，否则构造线程就炸了
        self._u = _user32() if IS_WINDOWS else None

    def run(self) -> None:
        if self._u is None:      # 非 Windows：无焦点可护卫，安静退出
            return
        u = self._u
        self.fails = 0
        try:
            self.last_user_hwnd = int(u.GetForegroundWindow())
        except Exception:
            pass
        while not self._stop.is_set():
            try:
                h = int(u.GetForegroundWindow())
                if h and h != self.last_user_hwnd:
                    r = window_rect(h)
                    if r and r[0] <= -30000:
                        # 屏幕外窗口抢了前台 —— 还回去
                        if self.last_user_hwnd and u.IsWindow(self.last_user_hwnd):
                            if restore_foreground(self.last_user_hwnd):
                                self.fixes += 1
                            else:
                                # 还不动就别每 0.8 秒试一次（Alt 键会被反复按），
                                # 退避 2 秒，连续 30 次不成功就放弃（说明系统不给）。
                                self.fails += 1
                                if self.fails >= 30:
                                    return
                                self._stop.wait(2.0)
                                continue
                    else:
                        self.last_user_hwnd = h
                        self.fails = 0
            except Exception:
                pass
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()


def _current_foreground_hwnd() -> int:
    """当前前台窗口句柄；非 Windows 返回 0。"""
    if not IS_WINDOWS:
        return 0
    try:
        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return 0


def start_focus_guard(interval: float = 0.8) -> FocusGuard:
    """起焦点护卫。非 Windows 上返回一个停用的空壳，调用方无需分平台分支。

    为什么要有空壳：main 里无论如何都会 `guard.stop()` 并读 `guard.fixes`，
    与其让每个调用点写 if，不如这里统一保证接口存在。
    """
    g = FocusGuard(interval)
    if IS_WINDOWS:
        g.start()
    return g
