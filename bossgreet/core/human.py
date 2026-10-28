# ============ 真实 OS 级输入：对抗 BOSS 前端风控 ============
# 直接复用自 boss-auto-apply 的实战结论：
#   Playwright 经 CDP 注入的键盘/鼠标事件带机器特征（事件时间戳节奏、
#   InputDeviceCapabilities、pointerType 等），BOSS 前端据此触发
#   _security_check 无限刷新。真人手动操作时事件来自真实硬件，特征截然不同。
#
#   本模块统一走 Windows 真实输入事件（pyautogui 底层 SendInput）：
#     - 中文：剪贴板粘贴（pyperclip），与「输入法上屏」事件特征一致
#     - 英文/数字：真实逐字键盘事件（带自然抖动）
#     - 鼠标：缓动轨迹 + 随机停顿 + 真实按下/松开
#
# 硬约束：OS 级事件只落在当前激活窗口 → 调用前必须先 focus_window()。

from __future__ import annotations

import random
import sys
import time

# ⚠ pyautogui / pyperclip 一律【惰性加载】，不在模块 import 时引入。
# 原因：二者在 import 时会要求有 GUI 会话与显示环境（Linux 无 DISPLAY、
# CI 容器、无头服务器），直接 import 会让整个包连「只读的过滤/去重逻辑」
# 都用不了 —— 而那些逻辑根本不需要键鼠。
# 只有在真正要用 OS 级输入（input_mode: os）时才 import。
_PG = None
_PC = None
_PG_ERR = ""


def _pyautogui():
    """按需拿到 pyautogui。不可用时抛出带说明的 RuntimeError，而不是在 import 期爆炸。"""
    global _PG, _PG_ERR
    if _PG is not None:
        return _PG
    try:
        import pyautogui as _mod
        # 关闭安全栅栏（移到屏幕角落会抛异常），坐标全程由我们控制
        _mod.FAILSAFE = False
        _mod.PAUSE = 0
        _PG = _mod
    except Exception as e:              # ImportError 或 GUI 环境缺失
        _PG_ERR = f"{type(e).__name__}: {e}"
        raise RuntimeError(
            "OS 级输入需要 pyautogui 与可用的图形会话，当前环境加载失败。"
            f"（{_PG_ERR}）请改用 input_mode: cdp（默认），它不需要 pyautogui。")
    return _PG


def _pyperclip():
    global _PC
    if _PC is not None:
        return _PC
    try:
        import pyperclip as _mod
    except Exception as e:
        raise RuntimeError(
            f"OS 级输入中文需要 pyperclip，加载失败：{type(e).__name__}: {e}")
    _PC = _mod
    return _PC


def os_input_available() -> bool:
    """当前环境能不能用 OS 级键鼠（给 doctor 体检与启动检查用）。"""
    if sys.platform != "win32":
        return False
    try:
        _pyautogui()
        _pyperclip()
        return True
    except Exception:
        return False


# ============ DPI 感知：整套坐标系统的地基（别删）============
# 踩坑记录（2026-10-13，实测确诊）：
#   不声明 DPI 感知时，Windows 会对本进程做「坐标虚拟化」——
#   pyautogui 的 SetCursorPos 用的坐标系，和浏览器 `window.screenX` /
#   `getBoundingClientRect()` 报告的 CSS 像素，**不是同一个**。
#   症状极具欺骗性：CDP 读到的坐标完全正确、页面 :hover 也会被点亮，
#   但真实点击永远落在偏移后的位置 → 点了没反应、详情面板不切换。
#
#   对照实验：同一段点击代码，加上本函数就生效，不加就不生效，其余完全相同。
#   必须在任何坐标读写之前调用，全进程只需一次。
def _enable_dpi_awareness() -> None:
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 2 = PER_MONITOR_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_enable_dpi_awareness()


def _sleep(a: float, b: float):
    time.sleep(random.uniform(a, b))


# ============ 窗口定位：必须精确命中「脚本所连的那个浏览器」 ============
# 踩坑记录：曾按标题含「BOSS/直聘」取 wins[0] —— 当系统里同时存在
#   Edge(带调试端口，脚本连的) 和 Chrome(用户日常，也有 BOSS 标签) 两个窗口时，
#   wins[0] 取决于 z-order，会随机把鼠标事件送到【另一个浏览器】。
#   症状极具迷惑性：CDP 能连、能读页面，但点什么都没反应。
# 现改为三重判定：浏览器 exe（由 CDP 的 UA 推出）+ 其实的页面标题 + 关键词回退。

_BROWSER_EXE: str | None = None


def browser_exe_from_ua(ua: str) -> str:
    """从 CDP 拿到的 navigator.userAgent 判断是 Edge 还是 Chrome。"""
    return "msedge.exe" if "Edg/" in (ua or "") else "chrome.exe"


def set_target_browser(exe: str | None):
    """由 BrowserSession.connect() 调用，锁定本次要操作哪个浏览器。"""
    global _BROWSER_EXE
    _BROWSER_EXE = (exe or "").lower() or None


def enum_browser_windows() -> list[dict]:
    """所有可见的 Chrome/Edge 顶层窗口：[{hwnd, pid, exe, title, minimized}]。"""
    import ctypes
    from ctypes import wintypes as wt

    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    out: list[dict] = []

    def _proc_name(pid: int) -> str:
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            n = wt.DWORD(512)
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return buf.value.rsplit("\\", 1)[-1]
        finally:
            k.CloseHandle(h)
        return ""

    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        b = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, b, n + 1)
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _proc_name(pid.value).lower()
        if exe in ("chrome.exe", "msedge.exe"):
            out.append({"hwnd": hwnd, "pid": pid.value, "exe": exe,
                        "title": b.value, "minimized": bool(u.IsIconic(hwnd))})
        return True

    u.EnumWindows(CB(cb), 0)
    return out


def _activate(hwnd: int) -> bool:
    """前置窗口，并【验证】它真的成了前台窗口。"""
    import ctypes
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    for _ in range(3):
        _force_foreground(hwnd)
        _sleep(0.25, 0.45)
        if user32.GetForegroundWindow() == hwnd:
            return True
    return user32.GetForegroundWindow() == hwnd


def focus_window(title_fragment: str = "BOSS直聘",
                 title_hint: str | None = None,
                 prefer_exe: str | None = None) -> bool:
    """把【脚本所连的那个浏览器】窗口提到前台。返回是否成功。

    判定顺序（越靠前越精确）：
      1. exe 匹配 + 窗口标题含当前页面标题（title_hint）——最可靠
      2. exe 匹配 + 标题含 BOSS/zhipin/直聘
      3. 仅 exe 匹配
    只有当窗口确实成为前台窗口时才返回 True —— 宁可返回 False 让上层中止，
    也绝不盲点（盲点会点到另一个浏览器上，甚至点到别的东西）。
    """
    try:
        exe = (prefer_exe or _BROWSER_EXE or "").lower() or None
        wins = enum_browser_windows()
        if not wins:
            return False

        # 严格按 exe 过滤：已知浏览器类型时，绝不允许命中另一个浏览器
        strict = [w for w in wins if w["exe"] == exe] if exe else list(wins)

        # 1) 用当前页面标题定位（截取前 12 字，避开「和另外 N 个页面」等后缀差异）
        if title_hint:
            key = title_hint.strip()[:12]
            if key:
                hit = [w for w in strict if key in w["title"]]
                if hit:
                    return _activate(hit[0]["hwnd"])

        # 2) 关键词（同样只在 strict 里找）
        for frag in (title_fragment, "BOSS", "zhipin", "直聘", "聘"):
            if not frag:
                continue
            hit = [w for w in strict if frag in w["title"]]
            if hit:
                return _activate(hit[0]["hwnd"])

        # 3) 就是我们连的那个浏览器 → 认
        if strict:
            return _activate(strict[0]["hwnd"])

        # 4) 实在不知道是哪个浏览器时，才在全量窗口里按关键词兜底
        for frag in (title_fragment, "BOSS", "zhipin", "直聘"):
            if not frag:
                continue
            hit = [w for w in wins if frag in w["title"]]
            if hit:
                return _activate(hit[0]["hwnd"])
        return False
    except Exception:
        return False


def _force_foreground(hwnd: int):
    """ctypes 调 Win32 API 强制前置（比 pygetwindow.activate 可靠）。"""
    import ctypes
    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    # SetForegroundWindow 可能被前台锁忽略，先模拟一次 Alt 按键绕过
    if not user32.GetForegroundWindow() == hwnd:
        user32.keybd_event(0x12, 0, 0, 0)   # Alt 按下
        user32.keybd_event(0x12, 0, 2, 0)   # Alt 抬起
        user32.SetForegroundWindow(hwnd)


def click_at(x: int, y: int, steps: int | None = None):
    """真实鼠标：缓动移动到 (x,y)，停顿后按下松开。"""
    pg = _pyautogui()
    duration = random.uniform(0.15, 0.45)
    pg.moveTo(x, y, duration=duration, tween=pg.easeOutQuad)
    _sleep(0.15, 0.5)
    pg.mouseDown(x, y)
    _sleep(0.06, 0.16)
    pg.mouseUp(x, y)
    _sleep(0.1, 0.3)


def type_chinese(text: str):
    """输入任意文本（含中文）：走剪贴板粘贴。

    真人用输入法上屏时，本质也是「系统把整块文字放进输入框」，
    事件特征与 Ctrl+V 一致，前端无法区分。
    """
    _pyperclip().copy(text)
    _sleep(0.15, 0.4)
    _pyautogui().hotkey("ctrl", "v")
    _sleep(0.3, 0.8)


def type_ascii(text: str, cpm: int = 220):
    """输入纯 ASCII：真实逐字键盘事件。cpm = 每分钟字符数。"""
    pg = _pyautogui()
    for ch in text:
        if ch.isupper():
            pg.keyDown("shift")
            pg.press(ch.lower())
            pg.keyUp("shift")
        else:
            pg.press(ch)
        time.sleep(60.0 / cpm * random.uniform(0.7, 1.4))


def clear_field():
    """清空输入框：全选 + 删除，走真实快捷键。"""
    pg = _pyautogui()
    pg.hotkey("ctrl", "a")
    _sleep(0.1, 0.3)
    pg.press("delete")
    _sleep(0.2, 0.5)


def press_enter():
    _pyautogui().press("enter")
    _sleep(0.3, 0.7)


def human_search(query: str):
    """一次真人式搜索输入：清空 → 输入关键词 → 回车。"""
    clear_field()
    if _is_ascii(query):
        type_ascii(query)
    else:
        type_chinese(query)
    _sleep(0.5, 1.3)
    press_enter()


def type_message(text: str):
    """把一段（长）消息录入当前焦点输入框。

    消息通常是中文长文本：先清空残留（防止上一条没发出去被拼在一起），
    再用剪贴板整段粘贴；粘贴后刻意停顿，模拟人写完看一眼再回车。
    回车由调用方决定是否发出（有些输入框需要 Shift+Enter 换行）。
    """
    clear_field()
    _sleep(0.2, 0.5)
    type_chinese(text)
    _sleep(0.6, 1.5)


def _is_ascii(s: str) -> bool:
    return all(ord(c) < 128 for c in s)
