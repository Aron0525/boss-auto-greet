# ============ 后台输入：用 CDP 派发鼠标/键盘事件（不动真实鼠标、不抢焦点）============
# 为什么需要它：
#   原来的 core/human.py 走 pyautogui（Windows SendInput 真实 OS 事件）——
#   抗风控最好，但**必须把浏览器窗口切到前台**，而且会占用物理鼠标。
#   用户要求「后台跑、别占用前端鼠标」，所以补一条 CDP 口径的输入通道。
#
# 实测结论（2026-10-13，BOSS 直聘，Edge）：
#   · 窗口【最小化】时，Chrome 会直接丢弃 CDP 鼠标事件 —— 页面收到 0 条 click。
#     所以必须让窗口处于「可见」状态（可以被别的窗口遮住，但不能最小化）。
#   · 窗口可见时事件正常送达，isTrusted=True，卡片点击能切换右侧详情面板。
#   · Input.* 域没有 enable 方法，调用它不会暴露调试器身份；
#     实测点击后页面存活 10/10 秒，未触发 BOSS 反调试。
#
# 坐标口径（容易搞错，务必记住）：
#   CDP 的 Input.dispatchMouseEvent 用的是 **视口 CSS 像素**，
#   不是 screenX/screenY 那套屏幕物理像素 —— 所以这里【不要】加 window.screenX。
from __future__ import annotations

import random
import time

from bossgreet.core.cdp import Cdp, CdpError


def _sleep(a: float, b: float):
    time.sleep(random.uniform(a, b))


# 视口坐标 + 命中元素（命中元素用来排查"点偏了"）
_VIEWPORT_JS = r"""
const el = %s;
if (!el) return null;
el.scrollIntoView({block: 'center', inline: 'nearest'});
const r = el.getBoundingClientRect();
if (r.width <= 0 || r.height <= 0) return null;
const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
const hit = document.elementFromPoint(cx, cy);
return {
  x: Math.round(cx), y: Math.round(cy),
  w: Math.round(r.width), h: Math.round(r.height),
  tag: (el.tagName || '') + '.' + String(el.className || '').slice(0, 30),
  hit: hit ? (hit.tagName || '') + '.' + String(hit.className || '').slice(0, 30) : '',
  text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 40),
};
"""


def viewport_point(cdp: Cdp, find_expr: str) -> dict | None:
    """元素在视口里的中心坐标（CDP 输入用这套坐标）。

    注意用 evaluate（而不是 evaluate_json）：下面这段已经自带 IIFE，
    若再交给 evaluate_json 就会被多包一层 → 里层 return 被丢掉 → 恒返回 None。
    """
    try:
        return cdp.evaluate("(() => {" + (_VIEWPORT_JS % find_expr) + "})()")
    except CdpError:
        return None


def click_at(cdp: Cdp, x: float, y: float, settle: tuple[float, float] = (0.3, 0.8)) -> bool:
    """在视口坐标 (x, y) 处派发一次完整的左键点击。"""
    base = {"x": int(x), "y": int(y), "button": "left", "clickCount": 1}
    try:
        cdp.call("Input.dispatchMouseEvent", {**base, "type": "mouseMoved"})
        _sleep(0.08, 0.18)
        cdp.call("Input.dispatchMouseEvent", {**base, "type": "mousePressed", "buttons": 1})
        _sleep(0.05, 0.12)
        cdp.call("Input.dispatchMouseEvent", {**base, "type": "mouseReleased", "buttons": 1})
    except CdpError:
        return False
    _sleep(*settle)
    return True


def wheel_at(cdp: "Cdp", x: float, y: float, delta_y: int = -400,
             times: int = 1, settle: tuple[float, float] = (0.5, 0.9)) -> bool:
    """在视口坐标 (x, y) 处派发滚轮事件，滚 `times` 次。

    为什么需要它：BOSS 聊天页的会话列表是**虚拟列表** —— `.user-list` 的
    scrollHeight == clientHeight（没有溢出），设 `scrollTop` 完全无效，
    只有真实的 wheel 事件才能让它加载后面的会话（2026-10-17 实测）。
    """
    try:
        cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved",
                                              "x": int(x), "y": int(y)})
        _sleep(0.1, 0.2)
        for _ in range(max(1, times)):
            cdp.call("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": int(x), "y": int(y),
                "deltaX": 0, "deltaY": int(delta_y)})
            _sleep(*settle)
    except CdpError:
        return False
    return True


def click_expr(cdp: Cdp, find_expr: str,
               settle: tuple[float, float] = (0.3, 0.8)) -> bool:
    """找到元素 → 取视口坐标 → 派发点击。"""
    info = viewport_point(cdp, find_expr)
    if not info:
        return False
    return click_at(cdp, info["x"], info["y"], settle)


def focus_element(cdp: Cdp, find_expr: str) -> bool:
    """把 DOM 焦点放到元素上（后台输入键盘前必须先做这一步）。"""
    try:
        return bool(cdp.evaluate_json(
            "const el = (%s); if (!el) return false; el.focus(); return true;" % find_expr))
    except CdpError:
        return False


def clear_element(cdp: Cdp, find_expr: str) -> bool:
    """清空输入框内容（contenteditable 与 input/textarea 都处理）。"""
    try:
        return bool(cdp.evaluate_json(
            "const el = (%s); if (!el) return false; el.focus();"
            "if (el.isContentEditable) {"
            "  const r = document.createRange(); r.selectNodeContents(el);"
            "  const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);"
            "  document.execCommand('delete', false, null);"
            "} else { el.value = ''; el.dispatchEvent(new Event('input', {bubbles: true})); }"
            "return true;" % find_expr))
    except CdpError:
        return False


def type_text(cdp: Cdp, text: str) -> bool:
    """把整段文本插进当前焦点元素。

    用 Input.insertText —— 中文直接可用，不需要剪贴板（这是相对 OS 模式的一大优势）。
    """
    if not text:
        return False
    try:
        cdp.call("Input.insertText", {"text": text})
    except CdpError:
        return False
    _sleep(0.2, 0.5)
    return True


def press_enter(cdp: Cdp) -> bool:
    """在当前焦点元素上按回车（BOSS 聊天框是「Enter 发送」）。"""
    key = {
        "key": "Enter", "code": "Enter", "text": "\r",
        "unmodifiedText": "\r",
        "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
    }
    try:
        cdp.call("Input.dispatchKeyEvent", {**key, "type": "keyDown"})
        _sleep(0.04, 0.10)
        cdp.call("Input.dispatchKeyEvent", {**key, "type": "keyUp", "text": ""})
    except CdpError:
        return False
    _sleep(0.3, 0.8)
    return True


def window_visibility(cdp: Cdp) -> dict:
    """窗口是否可见 —— CDP 输入要求「可见」（最小化时事件会被浏览器丢掉）。"""
    try:
        return cdp.evaluate_json(
            "return {visibility: document.visibilityState, hidden: document.hidden,"
            "        screenX: window.screenX, screenY: window.screenY};") or {}
    except CdpError:
        return {}
