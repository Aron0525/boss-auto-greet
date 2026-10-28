# ============ 页面对象层：BOSS 直聘（裸 CDP 版）============
# 架构约束（务必遵守，否则页面会被 BOSS 反调试干掉）：
#   1. 读页面一律走 core.cdp.Cdp.evaluate（只 Runtime.evaluate，不 enable 任何域）
#   2. 点击/输入一律走 core.human 的真实 OS 键鼠事件（pyautogui）
#      —— 既避开前端风控，也避开 CDP 输入事件的机器特征
#   3. 元素坐标 = 页面内 getBoundingClientRect + 窗口偏移（换算成屏幕物理像素）
from __future__ import annotations

import random
import re
import time
from typing import Any

from bossgreet.core.cdp import Cdp, CdpError
from bossgreet.core import cdpinput as bg
from bossgreet.core.salary import decode_salary
from bossgreet import cities as _cities
from bossgreet.core.human import (
    focus_window,
    click_at,
    type_message,
    press_enter,
    clear_field,
    type_ascii,
    type_chinese,
)


def _sleep(a: float, b: float):
    time.sleep(random.uniform(a, b))


# ---------- 输入通道（两种，由 config 的 browser.input_mode 决定）----------
#   "cdp" = 后台输入：CDP 派发事件，不动真实鼠标、不抢前台。
#           要求浏览器窗口【可见】（可被别的窗口遮住，但不能最小化）。
#   "os"  = 真实 OS 键鼠（pyautogui/SendInput）：抗风控更强，但会占用鼠标，
#           且必须把窗口切到前台。
# 默认 cdp（不占鼠标、不抢前台）。老版本默认是 os —— 只要有一个工具忘了调
# set_input_mode，它就会去动真实鼠标，所以默认值必须是最安全的那个。
INPUT_MODE = "cdp"

# 焦点模拟是否已经打开过（幂等，开一次就够）。窗口常年在屏幕外时，
# 页面一直是 hidden，靠它判断还要不要等那 0.4 秒。
_FOCUS_EMU = False


def set_input_mode(mode: str) -> str:
    global INPUT_MODE
    INPUT_MODE = "cdp" if str(mode).strip().lower() == "cdp" else "os"
    return INPUT_MODE


# ---------- 坐标换算 ----------

_COORD_JS = r"""
const el = %s;
if (!el) return null;
el.scrollIntoView({block: 'center', inline: 'center'});
const r = el.getBoundingClientRect();
const dpr = window.devicePixelRatio || 1;
const chromeX = (window.outerWidth  - window.innerWidth)  || 0;
const chromeY = (window.outerHeight - window.innerHeight) || 0;
return {
  x: Math.round((window.screenX + chromeX + r.left + r.width  / 2) * dpr),
  y: Math.round((window.screenY + chromeY + r.top  + r.height / 2) * dpr),
  w: Math.round(r.width), h: Math.round(r.height), dpr: dpr,
  text: (el.innerText || '').trim().slice(0, 40),
};
"""


def _norm(s: str) -> str:
    """归一化文本用于比较：去掉所有空白与全/半角标点。"""
    return re.sub(r"[\s·・,，。.、/\\|()（）\[\]【】\-—_]+", "", s or "")


# 坐标换算的公共前缀：把「视口内坐标」变成「屏幕物理坐标」。
# 注意：进程必须已声明 DPI 感知，否则这里算出来的数在 OS 层会被虚拟化（见 core/human.py）。
_COORD_PREFIX = (
    "const dpr = window.devicePixelRatio || 1;"
    "const cx = (window.outerWidth - window.innerWidth) || 0;"
    "const cy = (window.outerHeight - window.innerHeight) || 0;"
)


def element_screen_center(cdp: Cdp, find_expr: str) -> dict | None:
    """find_expr 是一段求值为 DOM 元素的 JS 表达式。返回 {x,y,...} 或 None。"""
    try:
        return cdp.evaluate("(() => {" + (_COORD_JS % find_expr) + "})()")
    except CdpError:
        return None


def focus_this(cdp: Cdp) -> bool:
    """让「后续的输入」能落到当前 BOSS 页上。

    · os 模式：把浏览器窗口切到前台（真实键鼠事件只落在前台窗口）。
    · cdp 模式：**不抢前台**（用户硬性要求：Edge 只能后台跑）。窗口被别的
      窗口完全遮挡时 Chromium 会把页面标成 hidden 并丢弃 CDP 鼠标事件 ——
      2026-10-17 晚实测：遮挡下「立即沟通」点了没反应、招呼没发出去。
      解法不是抢前台，而是把页面拉回 active（对用户完全无感）：
        Page.setWebLifecycleState('active') + Emulation.setFocusEmulationEnabled
      遮挡实测：这两招能让 visibility 从 hidden 恢复 visible、点击恢复生效。
    """
    if INPUT_MODE == "cdp":
        return ensure_interactive(cdp)

    try:
        hint = cdp.title()
    except Exception:
        hint = ""
    return focus_window(title_hint=hint)


def ensure_interactive(cdp: Cdp) -> bool:
    """确保页面能收 CDP 输入（不抢前台、不动窗口）。visible 或救活成功 → True。

    每次坐标点击前都会过这里（click_element / open_card 都调 focus_this）：
    visible 时只花一次 evaluate；hidden 时才动手救。

    2026-10-18：窗口默认被挪到屏幕外（见 core/winpos.py），页面因此常年是
    hidden —— 这套救活成了常态路径，所以把「已经开过焦点模拟」记下来，
    之后每次只补 lifecycle 状态 + 等 0.2s，别再每次干等 0.4s。
    """
    vis = bg.window_visibility(cdp)
    if not vis or vis.get("visibility") == "visible":
        return True
    # hidden：把页面拉回 active。两个调用都只作用于本页面，不碰窗口 z-order、
    # 不抢焦点，用户完全无感（2026-10-17 晚全屏遮挡、10-18 屏幕外均实测有效）。
    try:
        cdp.call("Page.setWebLifecycleState", {"state": "active"})
    except Exception:
        pass
    global _FOCUS_EMU
    if not _FOCUS_EMU:
        try:
            cdp.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
            _FOCUS_EMU = True
        except Exception:
            pass
    time.sleep(0.2 if _FOCUS_EMU else 0.4)
    vis2 = bg.window_visibility(cdp)
    return bool(vis2 and vis2.get("visibility") == "visible")


def click_element(cdp: Cdp, find_expr: str, settle=(0.5, 1.2), retry_focus=True) -> bool:
    """真实/后台点击某个 DOM 元素。按 INPUT_MODE 选通道。"""
    if not focus_this(cdp) and retry_focus:
        return False

    if INPUT_MODE == "cdp":
        info = bg.viewport_point(cdp, find_expr)
        if not info:
            return False
        return bg.click_at(cdp, info["x"], info["y"], settle)

    info = element_screen_center(cdp, find_expr)
    if not info:
        return False
    _sleep(0.2, 0.5)
    click_at(info["x"], info["y"])
    _sleep(*settle)
    return True


def type_into_field(cdp: Cdp, find_expr: str, text: str) -> bool:
    """按当前模式往输入框里打字（中文都支持，两条通道的实现不同）。"""
    if not focus_this(cdp):
        return False
    if INPUT_MODE == "cdp":
        # 后台：先把 DOM 焦点给输入框，再整段插入（不需要剪贴板）
        if not bg.clear_element(cdp, find_expr):
            return False
        _sleep(0.15, 0.35)
        return bg.type_text(cdp, text)
    # OS 模式：点进去 → 清空 → 真实键盘输入
    if not click_element(cdp, find_expr, settle=(0.4, 0.9)):
        return False
    clear_field()
    type_message(text)
    return True


# 聊天输入框的查找表达式（发定制消息用；SearchPage 的搜索框有自己的表达式）
_CHAT_INPUT_FIND = ("document.querySelector('#chat-input')"
                    " || document.querySelector('.chat-input')"
                    " || document.querySelector('div.chat-input')"
                    " || document.querySelector(\"[contenteditable='true']\")")


def press_enter_key(cdp: Cdp, find_expr: str = "") -> bool:
    """在输入框上按回车（聊天页「Enter 发送」，搜索页「回车搜索」）。

    2026-10-17 变体实测（scripts/_probe_variants.py，四种姿势对比）：
    CDP 的 `Input.dispatchKeyEvent` 派发 Enter，在【浏览器窗口不在前台】
    时会被页面静默丢弃（`document.hasFocus()===false`，渲染进程收不到
    键盘事件）—— 不点 / 坐标点 / JS 点三种前置姿势全部「超时未发」；
    而 JS 合成事件 `new KeyboardEvent('keydown', {key:'Enter', keyCode:13})`
    直接派发到输入框上，立刻就发出去了（BOSS 的处理器不检查 isTrusted）。
    ⇒ cdp 模式默认走 JS 派发；找不到元素时才退回 CDP 按键。
    （探针 1 曾「成功」是因为浏览器刚启动恰好在前台 —— 这也是同一套代码
    时好时坏的原因。）
    """
    if INPUT_MODE == "os":
        press_enter()
        return True
    expr = find_expr or _CHAT_INPUT_FIND
    try:
        ok = cdp.evaluate_json(
            "const el = (%s); if (!el) return false; el.focus();"
            "const ev = new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter',"
            "  keyCode: 13, which: 13, bubbles: true, cancelable: true});"
            "el.dispatchEvent(ev); return true;" % expr)
    except CdpError:
        ok = False
    if ok:
        _sleep(0.3, 0.8)
        return True
    return bg.press_enter(cdp)


def element_exists(cdp: Cdp, find_expr: str) -> bool:
    """元素是否存在且可见（两种输入通道通用，别直接调 element_screen_center）。"""
    if INPUT_MODE == "cdp":
        return bg.viewport_point(cdp, find_expr) is not None
    return element_screen_center(cdp, find_expr) is not None


# ---------- 搜索结果页 ----------

class SearchPage:
    """搜索结果页：https://www.zhipin.com/web/geek/jobs"""

    HOME = "https://www.zhipin.com/web/geek/jobs"

    # 城市码表统一放在 bossgreet.cities —— 过滤层展开「珠三角」时也要用它，
    # 只埋在页面对象里会让纯逻辑模块反依赖浏览器层。
    CITY_CODES = _cities.CITY_CODES

    CARD_SEL = "li.job-card-wrapper, li.job-card-box, .job-card-box"
    NO_RESULT_SEL = ".search-no-result, .no-result, .job-empty, .empty-tip"

    def __init__(self, cdp: Cdp):
        self.cdp = cdp

    # ---- 站点/风控 ----

    def url(self) -> str:
        try:
            return self.cdp.evaluate("location.href") or ""
        except CdpError:
            return ""

    def title(self) -> str:
        try:
            return self.cdp.title()
        except CdpError:
            return ""

    def on_target_site(self) -> bool:
        return "zhipin.com" in self.url()

    def is_refreshed(self) -> bool:
        u = self.url()
        return "_security_check" in u or (u and "zhipin.com" not in u)

    def login_hint(self) -> bool:
        """是否未登录。两个判据任一命中即认为未登录：
           1) 薪资栏绝大多数为空（BOSS 对未登录用户不给薪资）—— 用比例而非「全空」，
              因为列表末尾可能有 1 条推荐位带薪资
           2) 页面出现「立即登录 / 登录账号」等字样
        """
        try:
            r = self.cdp.evaluate_json(r"""
const s = document.querySelectorAll('.job-salary');
let empty = 0;
s.forEach(n => { if (!(n.innerText || '').trim()) empty++; });
const t = document.body ? (document.body.innerText || '') : '';
const asks = ['立即登录', '登录账号', '扫码登录', '请先登录'].some(k => t.includes(k));
return {total: s.length, empty: empty, asks: asks};
""") or {}
            total = r.get("total", 0)
            if total and r.get("empty", 0) / total >= 0.8:
                return True
            return bool(r.get("asks"))
        except CdpError:
            return False

    # ---- 读取岗位 ----

    def job_cards(self) -> list[dict]:
        """返回当前页所有岗位卡片（含解析字段与屏幕坐标）。"""
        js = r"""
const SEL = '%s';
const cards = Array.from(document.querySelectorAll(SEL));
const pick = (el, sels) => {
  for (const s of sels) {
    const n = el.querySelector(s);
    if (n && (n.innerText || '').trim()) return n.innerText.trim();
  }
  return '';
};
const dpr = window.devicePixelRatio || 1;
const chromeX = (window.outerWidth  - window.innerWidth)  || 0;
const chromeY = (window.outerHeight - window.innerHeight) || 0;
return cards.map((c, i) => {
  const r  = c.getBoundingClientRect();
  const a  = c.querySelector("a[href*='/job_detail/'], a.job-card-left, a.job-name, a");
  return {
    index:  i,
    title:   pick(c, ['.job-name', '.job-name-box', "[class*='job-name']"]),
    salary:  pick(c, ['.job-salary', '.salary']),
    company: pick(c, ['.boss-name', '.company-name', "[class*='company-name']"]),
    city:    pick(c, ['.company-location', '.job-area', "[class*='job-area']"]),
    tags:    Array.from(c.querySelectorAll('ul.tag-list li')).map(x => (x.innerText || '').trim()).filter(Boolean),
    href:    a ? (a.getAttribute('href') || '') : '',
    x: Math.round((window.screenX + chromeX + r.left + r.width  / 2) * dpr),
    y: Math.round((window.screenY + chromeY + r.top  + r.height / 2) * dpr),
  };
});
""" % self.CARD_SEL
        try:
            cards = self.cdp.evaluate("(() => {" + js + "})()") or []
        except CdpError:
            return []
        # 薪资在 DOM 里是字体混淆过的私有区码位，在这里就地还原成明文，
        # 下游（过滤 / 日志 / 投递清单）拿到的就是真数字。
        # 原文本留在 salary_raw 里备查。
        for c in cards:
            c["salary_raw"] = c.get("salary", "")
            c["salary"] = decode_salary(c.get("salary_raw", ""))
        return cards

    def card_count(self) -> int:
        try:
            return int(self.cdp.evaluate(f"document.querySelectorAll('{self.CARD_SEL}').length") or 0)
        except CdpError:
            return 0

    def has_no_result(self) -> bool:
        try:
            return bool(self.cdp.evaluate(f"!!document.querySelector('{self.NO_RESULT_SEL}')"))
        except CdpError:
            return False

    def wait_for_results(self, timeout_ms: int = 15000) -> bool:
        """等卡片出现。需要窗口在前台 —— BOSS 列表是可见性驱动的。"""
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            focus_this(self.cdp)
            if self.card_count() > 0:
                return True
            if self.has_no_result():
                return False
            time.sleep(0.6)
        return False

    # ---- 导航到搜索页 ----

    @classmethod
    def build_search_url(cls, city: str = "", query: str = "", page: int = 1) -> str:
        import urllib.parse
        qs = []
        code = cls.CITY_CODES.get(city, "")
        if code:
            qs.append(f"city={code}")
        if query:
            qs.append("query=" + urllib.parse.quote(query))
        if page and page > 1:
            qs.append(f"page={page}")
        return cls.HOME + ("?" + "&".join(qs) if qs else "")

    def open_search(self, city: str = "", query: str = "", page: int = 1,
                    timeout_ms: int = 20000) -> bool:
        """导航到「指定城市 + 关键词」的搜索页，成功返回 True。

        两个实测踩过的坑，缺一不可：
          1. **后台标签页的导航会被浏览器还原**（导航后 3~6 秒 URL 自己变回去，
             表现是"没报错但页面没换"）。2026-10-17 晚以前靠 bringToFront 激活
             标签来防，但 Page.bringToFront 会把窗口带到前台 —— 用户明确要求
             Edge 不能跳出来。现在改用 ensure_interactive（setWebLifecycleState
             + 焦点模拟）把页面拉活跃而不抢前台，导航落地后再复核一次 URL，
             被还原就重发（最多 3 轮）。
          2. **用 CDP 的 Page.navigate，别用 Playwright 的 goto**。后者会启用
             调试域，触发 BOSS 反调试，页面 2~3 秒内被杀。
        """
        url = self.build_search_url(city, query, page)
        want_city = bool(self.CITY_CODES.get(city))

        # 不抢前台地把页面拉活跃（替代原来的 bring_to_front）
        ensure_interactive(self.cdp)
        time.sleep(0.4)

        deadline = time.time() + timeout_ms / 1000.0
        for _ in range(3):
            try:
                self.cdp.call("Page.navigate", {"url": url})
            except Exception:
                pass
            # 等导航落地 + 卡片渲染
            while time.time() < deadline:
                time.sleep(0.6)
                if "zhipin.com" not in self.url():
                    continue
                if want_city and f"city={self.CITY_CODES[city]}" not in self.url():
                    break          # URL 没落到目标城市，重发
                if self.card_count() > 0:
                    # 防「后台标签页导航被还原」：落住后再等 2 秒复核 URL，
                    # 被还原了就重来。以前靠 bringToFront 防这个（会抢前台）。
                    time.sleep(2.0)
                    if "zhipin.com" not in self.url():
                        break
                    if want_city and f"city={self.CITY_CODES[city]}" not in self.url():
                        break
                    return True
        return self.card_count() > 0

    # ---- 页面内真实搜索（URL 导航失败时的回退）----

    def search(self, query: str) -> str:
        """在当前 BOSS 页上搜索：点搜索框 → 清空 → 真实输入 → 回车。

        返回 "ok" / "refreshed" / "no_box" / "fail"。
        """
        if not self.on_target_site():
            return "fail"
        find = ("""document.querySelector("input[name='query']") """
                """|| document.querySelector(".search-form input") """
                """|| document.querySelector("input[placeholder*='搜索']") """)
        if not element_screen_center(self.cdp, find):
            return "no_box"
        if not type_into_field(self.cdp, find, query):
            return "fail"
        _sleep(0.5, 1.2)
        press_enter_key(self.cdp, find)
        _sleep(1.5, 2.5)
        if self.is_refreshed():
            return "refreshed"
        return "ok"

    def card_point(self, index: int) -> dict | None:
        """按 DOM 索引取该卡片此刻的屏幕中心坐标（不滚动）。

        为什么必须"此刻"：列表会随点击自动滚动，一批读到的坐标只在当时的
        滚动位置下有效 —— 直接复用旧坐标会点到别的卡片或空白处。
        """
        return self.cdp.evaluate_json(
            "const cs = document.querySelectorAll('%s');"
            "const el = cs[%d]; if (!el) return null;"
            "const r = el.getBoundingClientRect();"
            "const dpr = window.devicePixelRatio || 1;"
            "const cx = (window.outerWidth - window.innerWidth) || 0;"
            "const cy = (window.outerHeight - window.innerHeight) || 0;"
            "return {"
            "  x: Math.round((window.screenX + cx + r.left + r.width  / 2) * dpr),"
            "  y: Math.round((window.screenY + cy + r.top  + r.height / 2) * dpr),"
            "  vx: Math.round(r.left + r.width / 2),"
            "  vy: Math.round(r.top  + r.height / 2),"
            "  in_view: r.top >= 0 && r.bottom <= window.innerHeight,"
            "};" % (self.CARD_SEL, index))

    def detail_name(self) -> str:
        """右侧详情面板当前显示的岗位名。"""
        try:
            return (self.cdp.evaluate_json(
                "const n = document.querySelector('.job-detail-info .name, "
                ".job-detail-box .job-name, .job-detail-container h1, "
                "[class*=\"job-detail\"] [class*=\"name\"]');"
                "return n ? (n.innerText || '').trim() : '';") or "")
        except CdpError:
            return ""

    def _detail_matches(self, title: str, timeout_ms: int = 4000) -> bool:
        """详情面板当前显示的，是不是这个岗位？

        2026-10-17 晚 A 类故障的修法：以前只比职位名【前 6 个字符】，
        「AI应用开发实习生」和「AI应用开发工程师」前缀相同 → 面板根本没
        切过去也判定成功，拿着上一条的 JD 继续跑。改成【完整标题双向
        包含】：卡片标题带 (J32188) 之类的编号后缀、面板标题更短都能匹配，
        但前缀撞车的一律判不匹配 → 触发 JS 点击兜底 → 再不符就跳过（安全
        方向：宁可跳过，也不能拿错 JD 发招呼）。
        """
        t = _norm(title)
        if not t:
            return True
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            cur = _norm(self.detail_name())
            if cur and (t in cur or cur in t):
                return True
            time.sleep(0.4)
        return False

    def open_card(self, card: dict) -> bool:
        """点开岗位卡片，让右侧详情面板切到它。返回是否真的切换成功。

        四步都不可省（每一步都踩过坑）：
          1. 按 DOM 索引 scrollIntoView —— 视口外的卡片点不到，
             视口底边附近的卡片也会因为换算出界而点空
          2. 重新取【实时】屏幕坐标 —— 滚动后旧坐标全部失效
          3. 点完校验详情面板是否真的换了 —— 否则后面会拿着上一条的 JD
             去生成消息，静默出错（最难发现的那种）
          4. 坐标点击没生效就 JS click 兜底 —— 窗口被拖出屏幕时 CDP 鼠标
             事件被静默丢弃，只有 DOM 通道还能点得动（2026-10-17 晚实测）
        """
        idx = card.get("index")
        if idx is None:
            return False

        ok = self.cdp.evaluate_json(
            "const cs = document.querySelectorAll('%s');"
            "const el = cs[%d]; if (!el) return false;"
            "el.scrollIntoView({block: 'center', inline: 'nearest'});"
            "return true;" % (self.CARD_SEL, idx))
        if not ok:
            return False
        time.sleep(0.5)

        pt = self.card_point(idx)
        if not pt:
            return False
        if not focus_this(self.cdp):
            return False
        if INPUT_MODE == "cdp":
            # 后台模式用视口坐标（vx/vy），屏幕坐标那套只有 OS 模式才用
            bg.click_at(self.cdp, pt["vx"], pt["vy"])
        else:
            click_at(pt["x"], pt["y"])
        _sleep(0.6, 1.0)

        if not self._detail_matches(card.get("title") or ""):
            # 坐标点击没生效 → JS click 兜底（2026-10-17 晚实测的坑）：
            # 浏览器窗口被拖到屏幕左外侧时（screenX≈-1000），视口坐标换算出的
            # 物理位置整个离屏，CDP 鼠标事件被静默丢弃 —— 卡片点了没反应、
            # 详情面板纹丝不动，但 elementFromPoint 又确实命中卡片（纯 DOM 算的）。
            # JS el.click() 走 DOM 通道，不受窗口位置影响。搜索下拉框那次
            # （坐标全败、JS 全成）是同一个病，same medicine。
            self._js_click_card(idx)
            _sleep(0.6, 1.2)

        return self._detail_matches(card.get("title") or "")

    def _js_click_card(self, idx: int) -> bool:
        """用 JS 直接 click 第 idx 张卡（坐标点击的兜底通道）。"""
        try:
            return bool(self.cdp.evaluate_json(
                "const cs = document.querySelectorAll('%s');"
                "const el = cs[%d]; if (!el) return false;"
                "el.scrollIntoView({block: 'center', inline: 'nearest'});"
                "el.click(); return true;" % (self.CARD_SEL, idx)))
        except CdpError:
            return False

    def scroll_list_bottom(self) -> bool:
        """把岗位列表滚到底，触发 BOSS 的懒加载。

        ★ 2026-10-15 实测：BOSS 的岗位列表是【滚动无限加载】，不是分页：
            初始 15 条 → 每滚到底一次 +15 条（15/30/45/60/75/90…）
          所以正确做法是「滚到底接着点」，而不是反复重开搜索页 URL。
          重开 URL 既有整页刷新（肉眼可见一直在闪），又慢得多。
        """
        js = r"""
(() => {
  const sel = '%s';
  const cs = document.querySelectorAll(sel);
  let el = cs.length ? cs[0] : null, sc = null;
  while (el && el !== document.body) {
    const st = getComputedStyle(el);
    if (/(auto|scroll)/.test(st.overflowY) && el.scrollHeight > el.clientHeight + 20) {
      sc = el; break;
    }
    el = el.parentElement;
  }
  if (sc) { sc.scrollTop = sc.scrollHeight; return 'container'; }
  window.scrollTo(0, document.body.scrollHeight);
  return 'window';
})()
""" % self.CARD_SEL
        try:
            self.cdp.evaluate(js)
            return True
        except CdpError:
            return False

    def load_more_by_scroll(self, rounds: int = 4) -> bool:
        """滚到底加载更多；返回「卡片数是否真的增加了」。

        返回 False = 滚到底也没有新卡片，说明该搜索词的池子已经掏空。
        """
        before = self.card_count()
        for _ in range(max(1, rounds)):
            self.scroll_list_bottom()
            _sleep(0.7, 1.3)
            if self.card_count() > before:
                return True
        return self.card_count() > before

    def goto_next_page(self) -> bool:
        find = ("""document.querySelector("a[ka='page-next']") """
                """|| document.querySelector(".pagination a.next") """
                """|| Array.from(document.querySelectorAll('a')).find(a => (a.innerText||'').trim() === '下一页')""")
        if not element_screen_center(self.cdp, find):
            return False
        if not click_element(self.cdp, find, settle=(1.2, 2.0)):
            return False
        return self.wait_for_results(12000)


# ---------- 详情 / 聊天 ----------

class JobDetailPage:
    """职位详情（右侧面板或独立详情页）+ 聊天发送。"""

    JD_SELECTORS = [".job-detail-body", ".job-detail-box", ".job-detail-container",
                    ".job-sec-text", ".job-detail-section", ".job-detail-content",
                    "[class*='job-detail'] .text", "[class*='detail-content']"]
    # 实测（2026-10 线上）：<a class="op-btn op-btn-chat" ka="cpc_job_list_chat_...">立即沟通</a>
    # 旧版是 .btn-startchat，两种都留着兜底。
    CHAT_SELECTORS = ["a.op-btn-chat", ".op-btn-chat", "[class*='op-btn-chat']",
                      "a[ka*='chat']", "a.btn-startchat", ".btn-startchat",
                      "a.btn-start-chat", "[class*='startchat']", "[class*='start-chat']"]
    INPUT_SELECTORS = ["#chat-input", ".chat-input", "div.chat-input",
                       ".chat-conversation textarea", "textarea.input-area",
                       "[contenteditable='true']"]

    def __init__(self, cdp: Cdp):
        self.cdp = cdp

    @staticmethod
    def _js_find(selectors: list[str]) -> str:
        parts = " || ".join(f"document.querySelector({s!r})" for s in selectors)
        return parts or "null"

    @staticmethod
    def _js_find_visible(selectors: list[str]) -> str:
        """在候选选择器里挑出【第一个真正可见】的元素。

        列表卡片上也有一个同名的「立即沟通」（悬停才显示，静态尺寸为 0），
        直接取 querySelector 的第一个会点到隐藏元素 → 必须过滤 visibility。
        """
        sels = "[" + ", ".join(repr(s) for s in selectors) + "]"
        return ("(() => {"
                f"  const sels = {sels};"
                "  const els = [];"
                "  for (const s of sels) for (const e of document.querySelectorAll(s)) els.push(e);"
                "  const vis = e => { const b = e.getBoundingClientRect();"
                "                     return b.width > 8 && b.height > 8; };"
                "  let el = els.find(vis);"
                "  if (!el) el = Array.from(document.querySelectorAll('a,button,span,div'))"
                "     .find(e => (e.innerText || '').trim() === '立即沟通' && vis(e));"
                "  return el || null;"
                "})()")

    def read_jd_text(self, limit: int = 1500) -> str:
        """读 JD 正文（多选择器兜底，取第一个有内容的）。

        注意：未登录时 BOSS 会把 JD 截断并附上「登录查看完整内容」——本方法会
        去掉这行提示，方便技术栈匹配；登录后可拿到全文。
        """
        try:
            txt = self.cdp.evaluate_json(
                "const sels = %s;"
                "for (const s of sels) {"
                "  const n = document.querySelector(s);"
                "  if (n) { const t = (n.innerText || '').trim();"
                "           if (t.length > 20) return t.slice(0, %d); }"
                "}"
                "return '';" % (self.JD_SELECTORS, limit))
        except CdpError:
            return ""
        txt = (txt or "").split("登录查看完整内容")[0]
        return txt.strip()

    def chat_button(self) -> dict | None:
        """「立即沟通」按钮的屏幕坐标；找不到返回 None。"""
        return element_screen_center(self.cdp, self._js_find_visible(self.CHAT_SELECTORS))

    def chat_button_text(self) -> str:
        """「立即沟通」按钮当前显示的文字。

        为什么需要它（2026-10-17 晚实测两次假成功）：
          · 没沟通过的岗位 → 按钮是「立即沟通」，点了才发招呼语；
          · 已沟通过的岗位（同 HR 今天打过招呼）→ 按钮变成「继续沟通」，
            点了只是打开旧会话，【不会】再发招呼 —— 但旧代码点了就记
            「已打招呼」，两家假成功就是这么来的。
          · 招呼发出去后按钮会就地翻转成「继续沟通」—— 也可当发送成功的佐证。
        """
        try:
            return (self.cdp.evaluate_json(
                "const el = %s; return el ? (el.innerText || '').trim() : '';"
                % self._js_find_visible(self.CHAT_SELECTORS)) or "")
        except CdpError:
            return ""

    def has_login_wall(self) -> bool:
        try:
            return bool(self.cdp.evaluate_json(r"""
const sels = ['.login-dialog', '.sign-form', '.login-register',
              '.boss-login-dialog', '.dialog-login'];
if (sels.some(s => document.querySelector(s))) return true;
const t = document.body ? (document.body.innerText || '') : '';
return /扫码登录|手机号登录|请先登录|登录\/注册/.test(t);
"""))
        except CdpError:
            return False

    def open_chat(self, timeout_ms: int = 8000) -> bool:
        """点「立即沟通」并等聊天输入框出现。"""
        find = self._js_find_visible(self.CHAT_SELECTORS)
        if not element_exists(self.cdp, find):
            return False
        if not click_element(self.cdp, find, settle=(1.2, 2.2)):
            return False
        return self.wait_for_input(timeout_ms)

    def wait_for_input(self, timeout_ms: int = 8000) -> bool:
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            if element_screen_center(self.cdp, self._js_find(self.INPUT_SELECTORS)):
                return True
            time.sleep(0.5)
        return False

    def chat_input(self) -> dict | None:
        return element_screen_center(self.cdp, self._js_find(self.INPUT_SELECTORS))

    def input_text(self) -> str:
        try:
            return self.cdp.evaluate_json(r"""
const el = document.querySelector('#chat-input, .chat-input, textarea.input-area, [contenteditable="true"]');
if (!el) return '';
return (el.value !== undefined && el.value !== null) ? el.value : (el.innerText || '');
""") or ""
        except CdpError:
            return ""

    # ---- 发送 ----

    def send_message(self, text: str) -> tuple[bool, str]:
        """把定制消息发出去。返回 (是否成功, 说明)。"""
        if self.chat_input() is None:
            return False, "找不到聊天输入框"
        if not focus_this(self.cdp):
            return False, "无法把 BOSS 浏览器窗口切到前台（真实键鼠事件要求窗口在前台）"

        find = self._js_find(self.INPUT_SELECTORS)
        if not type_into_field(self.cdp, find, text):
            return False, "点击/输入聊天输入框失败"
        _sleep(0.4, 1.0)
        press_enter_key(self.cdp, find)
        _sleep(1.2, 2.2)

        if not self.input_text().strip():
            return True, "已发送（输入框已清空）"
        return False, "发送后输入框仍有内容，可能未发出"

    # ---- 「立即沟通」与打招呼弹窗 ----

    def click_chat_button(self) -> bool:
        """只点「立即沟通」，不等输入框。

        实测（2026-10-13，登录态）：点完后 **不会** 出现聊天输入框 ——
        BOSS 会先自动发出账号里设置的默认招呼语（自动带职位名，如
        「您好，请问{职位}还在招吗？…」），然后弹出一个 **发送成功提示框**
        （`.greet-boss-dialog`，里面是「留在此页继续沟通」）。
        定制消息必须之后到聊天页 `/web/geek/chat` 里发。
        """
        find = self._js_find_visible(self.CHAT_SELECTORS)
        if not element_exists(self.cdp, find):
            return False
        return click_element(self.cdp, find, settle=(1.5, 2.5))

    def greet_dialog_open(self) -> bool:
        try:
            return bool(self.cdp.evaluate("!!document.querySelector('.greet-boss-dialog')"))
        except CdpError:
            return False

    def dismiss_greet_dialog(self, timeout_ms: int = 8000) -> bool:
        """关掉「已向BOSS发送消息」提示框：点里面的「留在此页」，返回是否关掉。

        实测 DOM（2026-10-13）：
            .greet-boss-dialog
              └ .greet-boss-container
                  ├ .greet-boss-header   (h3 + span.close > i.icon-close)
                  └ .greet-boss-footer   ← innerText 是 '留在此页继续沟通'
                      ├ a.default-btn.cancel-btn  '留在此页'   ← 要点这个
                      └ a.default-btn.sure-btn    '继续沟通'   （跳到聊天页）

        ⚠ 踩过的坑：`.greet-boss-footer` 的 innerText 也是 '留在此页继续沟通'
        （把两个按钮的文字拼起来了），而且它是父节点、在 document 序里更靠前。
        用 `includes('留在此页')` 在 a/button/span/div 里找，会先命中 footer ——
        点它的中心正好落在两个按钮中间，**什么都不会发生**，弹窗一直留在那里。
        所以必须【文本精确相等】+【取面积最小的那个】（= 最深层、最可能是真按钮）。
        """
        if not self._wait_dialog(timeout_ms):
            return False

        # 首选：精确文本「留在此页」，面积最小者胜出
        find = ("(() => {"
                "  const root = document.querySelector('.greet-boss-dialog');"
                "  if (!root) return null;"
                "  const els = [...root.querySelectorAll('a,button,span,div')]"
                "      .filter(e => (e.innerText || '').trim() === '留在此页');"
                "  if (!els.length) return null;"
                "  els.sort((a, b) => {"
                "    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();"
                "    return ra.width * ra.height - rb.width * rb.height; });"
                "  return els[0];"
                "})()")
        if element_exists(self.cdp, find):
            click_element(self.cdp, find, settle=(0.5, 0.9))
            if not self.greet_dialog_open():
                return True

        # 兜底：右上角的 ×
        close = ("(() => { const r = document.querySelector('.greet-boss-dialog');"
                 "  if (!r) return null;"
                 "  return r.querySelector('.icon-close, .close, [class*=close]'); })()")
        if element_exists(self.cdp, close):
            click_element(self.cdp, close, settle=(0.5, 0.9))
        return not self.greet_dialog_open()

    def _wait_dialog(self, timeout_ms: int = 8000) -> bool:
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            if self.greet_dialog_open():
                return True
            time.sleep(0.25)
        return False

    # ---- 平台岗位适配拦截 ----

    def profile_mismatch_open(self) -> bool:
        """BOSS 明确提示工作经历不匹配时，不能把它误判为发送结果不确定。"""
        try:
            return bool(self.cdp.evaluate_json("""
(() => {
  const visible = e => {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 20 && r.height > 20 && s.display !== 'none'
      && s.visibility !== 'hidden' && s.opacity !== '0';
  };
  return [...document.querySelectorAll('div,section,dialog')].some(e =>
    visible(e) && /工作经历不匹配|请选择其他职位沟通/.test(e.innerText || '')
  );
})()
"""))
        except CdpError:
            return False

    def dismiss_profile_mismatch(self) -> bool:
        """仅关闭提示层；绝不点击“个人中心”或绕过岗位适配限制。"""
        if not self.profile_mismatch_open():
            return True
        close = ("(() => {"
                 " const visible = e => { const r=e.getBoundingClientRect(), s=getComputedStyle(e);"
                 "   return r.width>4 && r.height>4 && s.display!=='none' && s.visibility!=='hidden'; };"
                 " const roots=[...document.querySelectorAll('div,section,dialog')]"
                 "   .filter(e => visible(e) && /工作经历不匹配|请选择其他职位沟通/.test(e.innerText||''));"
                 " for (const root of roots) {"
                 "   const found=[...root.querySelectorAll('[aria-label*=关闭], .icon-close, .close, [class*=close]')]"
                 "     .filter(visible); if (found.length) return found[0];"
                 " } return null; })()")
        if element_exists(self.cdp, close):
            click_element(self.cdp, close, settle=(0.4, 0.8))
            time.sleep(0.3)
            if not self.profile_mismatch_open():
                return True
        # Escape 是关闭本地提示层的常规操作；未消失则交给上层安全停止。
        try:
            for event_type in ("keyDown", "keyUp"):
                self.cdp.call("Input.dispatchKeyEvent", {
                    "type": event_type, "key": "Escape", "code": "Escape",
                    "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27,
                    "modifiers": 0, "text": "",
                })
        except CdpError:
            return False
        time.sleep(0.4)
        return not self.profile_mismatch_open()

    # ---- 「今日沟通次数」温馨提示 / 已达上限（2026-10-17 深夜故障根因）----

    def chat_block_open(self) -> bool:
        """「今日沟通次数」弹窗是否开着（含软提示和已达上限两种态）。

        实测（2026-10-17 深夜）：
          · 软提示态（沟通 120 人后、额度未满）：
            温馨提示 / 您今天已与120位BOSS沟通，还剩30次沟通机会哦 / [好]
            → 点「好」关掉遮罩后再点一次「立即沟通」照常能发。
          · 已达上限态（150/天用完）：
            您已达到沟通上限 / 您今天已与150位BOSS沟通，休息一下，明天再来吧～ / [确定]
            → 点了也白点，必须等明天（按北京时间 0 点刷新）。
        两种态都是同一个 .chat-block-dialog + .chat-block-layer 遮罩，
        遮罩会吞掉之后所有坐标点击 —— 不关掉它整批全废。
        """
        try:
            return bool(self.cdp.evaluate(
                "!!document.querySelector('.chat-block-dialog')"))
        except CdpError:
            return False

    def chat_block_text(self) -> str:
        """弹窗正文（用来区分「还剩 X 次」和「已达上限」）。"""
        try:
            return (self.cdp.evaluate_json(
                "const d = document.querySelector('.chat-block-dialog');"
                "return d ? (d.innerText || '').trim() : '';") or "")
        except CdpError:
            return ""

    def chat_block_exhausted(self) -> bool:
        """今日额度是否真的用完了（150/天）。

        判据用文案不用次数：软提示说"还剩30次沟通机会哦"，
        耗尽态说"已达到沟通上限…明天再来吧"。按钮也会从「好」变成「确定」。
        """
        t = self.chat_block_text()
        return ("已达到沟通上限" in t) or ("明天再来" in t) or ("沟通上限" in t)

    def dismiss_chat_block(self, timeout_ms: int = 4000) -> bool:
        """点掉弹窗里的确认按钮（软提示是「好」，已达上限是「确定」），
        返回是否关掉了（含遮罩一起消失）。

        实测 DOM：
            .chat-block-dialog
              ├ .chat-block-layer            ← 遮罩，吞坐标点击的元凶
              └ .chat-block-container
                  ├ .chat-block-header  (h3 温馨提示 / 您已达到沟通上限)
                  ├ .chat-block-body    (p  …)
                  └ .chat-block-footer
                      └ a.default-btn.sure-btn  「好」/「确定」   ← 点这个
        和 dismiss_greet_dialog 同一个坑：footer 的 innerText 会把按钮文字
        拼在一起，必须【文本精确相等】+【面积最小】。
        """
        if not self.chat_block_open():
            return True
        # 首选：footer 里文本精确「好」或「确定」、面积最小的元素
        find = ("(() => {"
                "  const root = document.querySelector('.chat-block-dialog');"
                "  if (!root) return null;"
                "  const els = [...root.querySelectorAll('a,button,span,div')]"
                "      .filter(e => ['好', '确定'].includes((e.innerText || '').trim()));"
                "  if (!els.length) return null;"
                "  els.sort((a, b) => {"
                "    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();"
                "    return ra.width * ra.height - rb.width * rb.height; });"
                "  return els[0];"
                "})()")
        if element_exists(self.cdp, find):
            click_element(self.cdp, find, settle=(0.5, 0.9))
            time.sleep(0.4)
            if not self.chat_block_open():
                return True
        # 兜底：右上角 × / close
        close = ("(() => { const r = document.querySelector('.chat-block-dialog');"
                 "  if (!r) return null;"
                 "  return r.querySelector('.icon-close, .close, [class*=close]'); })()")
        if element_exists(self.cdp, close):
            click_element(self.cdp, close, settle=(0.5, 0.9))
            time.sleep(0.4)
        # 再兜底：JS 合成点击（遮罩还在时坐标点不动它）
        if self.chat_block_open():
            try:
                self.cdp.evaluate_json(
                    "const d = document.querySelector('.chat-block-dialog');"
                    "if (!d) return false;"
                    "let best = null;"
                    "d.querySelectorAll('a,button,span,div').forEach(n => {"
                    "  const t = (n.innerText || '').trim();"
                    "  if (t !== '好' && t !== '确定') return;"
                    "  const r = n.getBoundingClientRect();"
                    "  if (r.width < 4 || r.height < 4) return;"
                    "  const a = r.width * r.height;"
                    "  if (!best || a < best.a) best = {a: a, n: n};"
                    "});"
                    "if (best) { best.n.click(); return true; }"
                    "return false;")
            except CdpError:
                return False
            time.sleep(0.4)
        return not self.chat_block_open()


# ---------- 聊天页 ----------
class ChatPage:
    """聊天页 `https://www.zhipin.com/web/geek/chat` —— 真正发定制消息的地方。

    为什么定制消息不能在职位列表页发：列表页上根本没有聊天输入框
    （`[contenteditable]` 命中数为 0）。BOSS 把聊天放在独立页面。
    """

    URL = "https://www.zhipin.com/web/geek/chat"
    LIST_ITEM_SEL = ".user-list li"
    SEARCH_SEL = ".boss-search-input"

    INPUT_SELECTORS = ["#chat-input", ".chat-input", "div.chat-input",
                       "[contenteditable='true']"]

    def __init__(self, cdp: Cdp):
        self.cdp = cdp

    @staticmethod
    def _js_find(selectors: list[str]) -> str:
        parts = " || ".join(f"document.querySelector({s!r})" for s in selectors)
        return parts or "null"

    # ---- 导航 ----

    def goto(self, timeout_ms: int = 20000) -> bool:
        # 不抢前台（用户要求 Edge 不能跳出来）：用 ensure_interactive 把页面
        # 拉活跃，替代原来的 bring_to_front。
        ensure_interactive(self.cdp)
        time.sleep(0.4)
        for _ in range(3):
            try:
                self.cdp.call("Page.navigate", {"url": self.URL})
            except Exception:
                pass
            if self.wait_list(timeout_ms // 3):
                return True
        return False

    def on_chat_page(self) -> bool:
        try:
            return "/web/geek/chat" in (self.cdp.url() or "")
        except Exception:
            return False

    def wait_list(self, timeout_ms: int = 8000) -> bool:
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            try:
                if self.cdp.evaluate(
                        f"document.querySelectorAll('{self.LIST_ITEM_SEL}').length") > 0:
                    return True
            except CdpError:
                pass
            time.sleep(0.5)
        return False

    def conversations(self, limit: int = 12) -> list[dict]:
        """会话列表（按时间倒序，0 为最新）。

        limit 可以给很大（如 500）—— 补发场景要在上百个会话里按公司名找，
        默认的 12 个只够「刚打过招呼的一定在最前面」那种用法。
        """
        try:
            return self.cdp.evaluate_json(
                "const out = [];"
                "document.querySelectorAll('%s').forEach((el, i) => {"
                "  if (i >= %d) return;"
                "  out.push({index: i, text: (el.innerText || '')"
                "      .replace(/\\s+/g, ' ').trim().slice(0, 120)});"
                "});"
                "return out;" % (self.LIST_ITEM_SEL, limit)) or []
        except CdpError:
            return []

    def scroll_list(self, times: int = 3) -> bool:
        """把左侧会话列表往下滚，触发懒加载更多会话。

        列表是虚拟滚动的：`.user-list` 的 scrollHeight == clientHeight（没有溢出），
        设 `scrollTop` 一点用都没有 —— 必须派发真实 wheel 事件（2026-10-17 实测，
        找真正的滚动容器这条路走不通，DOM 里就没有 overflow:auto 的祖先）。
        """
        pt = self.cdp.evaluate_json(
            "const el = document.querySelector('.chat-user') || "
            "document.querySelector('.user-list');"
            "if (!el) return null;"
            "const b = el.getBoundingClientRect();"
            "return {x: Math.round(b.left + Math.min(b.width / 2, 160)),"
            "        y: Math.round(b.top + Math.min(b.height / 2, 320))};")
        if not pt:
            return False
        return bg.wheel_at(self.cdp, pt["x"], pt["y"], delta_y=-500, times=times)

    def find_conversation(self, match: str, rounds: int = 12) -> int | None:
        """在整个会话列表里找文本包含 match 的会话，返回 index；找不到返回 None。

        找不到就滚一次列表再找（虚拟滚动，一次滚出一批）。
        """
        for _ in range(max(1, rounds)):
            for conv in self.conversations(limit=500):
                if match and match in conv["text"]:
                    return conv["index"]
            if not self.scroll_list():
                return None
        return None

    # ---- 搜索定位 ----

    # 搜索结果下拉（不在左侧列表里，必须点这个才能打开会话）
    SEARCH_RESULT_SEL = ".boss-search-result .search-list"
    # 最近一次 search_and_open 点开的那条搜索结果文本。
    # 调用方靠它校验「开的确实是这家」—— 脱敏公司名
    # （「深圳某大型网络安全与云计算上市公司」）只能靠这个对上号。
    last_hit_text = ""

    def _type_keys(self, text: str) -> bool:
        """把关键词写进搜索框并**触发页面的搜索逻辑**。

        为什么绕这么一圈（2026-10-17 全部实测过）：
          · `Input.dispatchKeyEvent` 逐字键盘事件 —— 页面完全无感，
            所有调用返回 True 但 value 一个字不进（页面还会把焦点抢回 BODY）。
          · `Input.insertText` —— value 能进，但只改值、不触发过滤。
          · 两者组合：insertText 写值 + 补派一个 `keyup` 和 `input` 事件，
            搜索下拉立刻出现（搜「示例企业」实测出结果）。
        """
        if not text:
            return False
        try:
            if not bg.type_text(self.cdp, text):
                return False
            self.cdp.evaluate_json(
                "const el=%s; if (!el) return false;"
                "const last = el.value.slice(-1) || 'a';"
                "el.dispatchEvent(new KeyboardEvent('keyup', {key: last, bubbles: true}));"
                "el.dispatchEvent(new Event('input', {bubbles: true}));"
                "el.dispatchEvent(new Event('change', {bubbles: true}));"
                "return true;" % f"document.querySelector({self.SEARCH_SEL!r})")
        except CdpError:
            return False
        _sleep(0.3, 0.6)
        return True

    def _search_box_point(self) -> dict | None:
        """取搜索框的视口中心坐标。

        ⚠️ 不要在这里 scrollIntoView：`inline: 'nearest'` 会把页面横向滚到极端位置，
        元素 left 直接变成 -1200 多，之后所有点击全打到视口外面（2026-10-17 实测，
        这就是第二轮补发 40 家全部「搜不到」的根因）。
        搜索框本来就在页面顶部，不需要滚。
        """
        try:
            return self.cdp.evaluate_json(
                "const el=%s; if (!el) return null;"
                "const r = el.getBoundingClientRect();"
                "if (r.width <= 0 || r.height <= 0) return null;"
                "return {x: Math.round(r.left + r.width / 2),"
                "        y: Math.round(r.top + r.height / 2)};"
                % f"document.querySelector({self.SEARCH_SEL!r})")
        except CdpError:
            return None

    def _clear_search_box(self) -> bool:
        """聚焦并清空搜索框。

        ⚠️ 两条路都走不通，别再试（2026-10-17 实测）：
          · 真实点击（Input.dispatchMouseEvent）—— 坐标确实命中 INPUT，
            但页面立刻把焦点抢回 BODY，之后键盘事件全部无效。
          · Ctrl+A + Backspace —— 会把 Ctrl 卡在按下状态。
        所以只用 JS：focus + 置空 value + 派发 input/change。
        """
        try:
            ok = self.cdp.evaluate_json(
                "const el=%s; if (!el) return false;"
                "el.focus(); el.value = '';"
                "el.dispatchEvent(new Event('input', {bubbles: true}));"
                "el.dispatchEvent(new Event('change', {bubbles: true}));"
                "return true;" % f"document.querySelector({self.SEARCH_SEL!r})")
        except CdpError:
            return False
        _sleep(0.15, 0.35)
        return bool(ok)

    def _search_result_items(self) -> list[dict]:
        """搜索下拉里的可见条目（只要文本），用来挑「对的」那条会话。

        返回 [] 的两种情况：下拉没出来、下拉结构不是「list 直接子元素」
        （后者由调用方退回旧行为）。
        """
        try:
            return self.cdp.evaluate_json(
                "const list = document.querySelector('%s');"
                "if (!list) return [];"
                "const vis = e => { const b = e.getBoundingClientRect();"
                "                return b.width > 4 && b.height > 4; };"
                "const out = [];"
                "for (const el of list.children) {"
                "  if (!vis(el)) continue;"
                "  const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();"
                "  if (t) out.push({text: t.slice(0, 140)});"
                "}"
                "return out;" % self.SEARCH_RESULT_SEL) or []
        except CdpError:
            return []

    def search_and_open(self, keyword: str, wait_s: float = 2.5) -> bool:
        """搜联系人（公司名/人名）并点开第一个搜索结果。

        这是给 30 天内历史会话补发消息的唯一可靠路径：左侧列表只渲染 45 条、
        滚不动也搜不得（`scroll_list` / wheel / scrollTop 全部无效，2026-10-17 实测），
        而页面自带的搜索框（placeholder「搜索30天内的联系人」）能直达任意会话，
        结果落在 `.boss-search-result .search-list` 下拉里 —— 点它才真的打开会话。

        返回 False 的两种情况：搜索无结果、点开了但右侧没出现输入框。
        """
        # 冷页面（刚 goto 完）第一次搜索常常等不出下拉 —— 2026-10-17 实测
        # 示例企业在 --limit 2 首轮就这么「搜不到」过（探针重跑同一关键词又是好的）。
        # 所以「清空 → 输入 → 等下拉」整套最多试两轮，第二轮前给页面热身时间。
        hit = None
        for attempt in (1, 2):
            if not self._clear_search_box():
                return False
            if not self._type_keys(keyword):
                return False
            deadline = time.time() + wait_s
            while time.time() < deadline:
                hit = self.cdp.evaluate_json(
                    "const el = document.querySelector('%s');"
                    "if (!el) return null;"
                    "const b = el.getBoundingClientRect();"
                    "if (b.width < 5 || b.height < 5) return null;"
                    "return {vx: Math.round(b.left + b.width / 2),"
                    "        vy: Math.round(b.top + b.height / 2),"
                    "        text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 60)};"
                    % self.SEARCH_RESULT_SEL)
                if hit:
                    break
                time.sleep(0.3)
            if hit:
                break
            if attempt == 1:
                _sleep(1.0, 2.0)
        if not hit:
            return False

        # 点开搜索结果。**优先用 JS el.click()**：下拉项常常挂在视口边缘甚至外面，
        # 坐标点击容易打到别处（2026-10-17 实测坐标点击全失败、JS click 全成功）。
        # 多条结果时，优先点【文本里带关键词】的那条：脱敏公司名
        # （「深圳某大型网络安全与云计算上市公司」）按公司名能搜到，
        # 但默认点第一条有可能点进别家同名职位名的会话 → 消息发错对象。
        items = self._search_result_items()
        pick = 0
        for i, it in enumerate(items):
            if keyword and keyword in (it.get("text") or ""):
                pick = i
                break
        clicked = False
        if items:
            try:
                clicked = bool(self.cdp.evaluate_json(
                    "const list = document.querySelector('%s');"
                    "if (!list) return false;"
                    # 过滤条件必须和 _search_result_items 完全一致（可见 + 有文本），
                    # 否则两边的索引会错开 → 点到别的条目上。
                    "const keep = e => { const b = e.getBoundingClientRect();"
                    "  if (b.width <= 4 || b.height <= 4) return false;"
                    "  return ((e.innerText || '').trim().length > 0); };"
                    "const items = [...list.children].filter(keep);"
                    "const el = items[%d]; if (!el) return false;"
                    "el.scrollIntoView({block: 'nearest'}); el.click(); return true;"
                    % (self.SEARCH_RESULT_SEL, pick)))
            except CdpError:
                clicked = False
        if not clicked:
            # 下拉结构不是「list 直接子元素」时退回点容器（旧行为）
            try:
                self.cdp.evaluate_json(
                    "const el = document.querySelector('%s'); if (!el) return false;"
                    "el.scrollIntoView({block: 'nearest'}); el.click(); return true;"
                    % self.SEARCH_RESULT_SEL)
            except CdpError:
                pass
        time.sleep(1.2)
        self.last_hit_text = (items[pick].get("text") or "") if items and pick < len(items) else ""
        if self.input_box() is None and hit["vx"] > 0 and hit["vy"] > 0:
            bg.click_at(self.cdp, hit["vx"], hit["vy"], settle=(1.2, 2.0))
            time.sleep(1.0)

        # 关掉搜索下拉：不关的话它会盖在会话列表上，且焦点可能还留在搜索框里，
        # 紧接着发消息时 Enter 会打进搜索框而不是聊天框（2026-10-17 实测
        # 13 家「发送后输入框仍有内容」就是这么来的）。
        self.close_search()
        return self.input_box() is not None

    def close_search(self) -> bool:
        """关掉聊天页的搜索下拉（Esc + 点关闭按钮）。"""
        try:
            for t in ("keyDown", "keyUp"):
                self.cdp.call("Input.dispatchKeyEvent", {
                    "type": t, "key": "Escape", "code": "Escape",
                    "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27,
                    "modifiers": 0, "text": ""})
        except CdpError:
            pass
        _sleep(0.2, 0.4)
        try:
            has_close = self.cdp.evaluate_json(
                "const el = document.querySelector('.boss-search-container .close');"
                "if (!el) return false;"
                "const b = el.getBoundingClientRect();"
                "return b.width > 3 && b.height > 3 ? {x: Math.round(b.left + b.width/2),"
                "        y: Math.round(b.top + b.height/2)} : false;")
            if has_close:
                bg.click_at(self.cdp, has_close["x"], has_close["y"], settle=(0.4, 0.8))
        except CdpError:
            pass
        _sleep(0.3, 0.6)
        return True

    def open_conversation(self, match: str = "", index: int = 0,
                          limit: int = 12) -> bool:
        """点开某个会话。给了 match 就优先找一个文本包含它的。

        刚打过招呼的会话一定在**最前面**（消息最新）；match 是保险丝，
        防止中途有人回消息把顺序顶乱。limit 控制搜索范围 ——
        补发历史会话时要传大值（或直接用 find_conversation 先定位）。
        """
        target = index
        if match:
            for conv in self.conversations(limit=limit):
                if match in conv["text"]:
                    target = conv["index"]
                    break
        info = self.cdp.evaluate_json(
            "const el = document.querySelectorAll('%s')[%d];"
            "if (!el) return null;"
            "el.scrollIntoView({block: 'nearest', inline: 'nearest'});"
            "const b = el.getBoundingClientRect();"
            "%s"
            "return {x: Math.round((window.screenX + cx + b.left + b.width / 2) * dpr),"
            "        y: Math.round((window.screenY + cy + b.top + b.height / 2) * dpr),"
            "        vx: Math.round(b.left + b.width / 2),"
            "        vy: Math.round(b.top + b.height / 2),"
            "        text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 60)};"
            % (self.LIST_ITEM_SEL, target, _COORD_PREFIX))
        if not info:
            return False
        if not focus_this(self.cdp):
            return False
        if INPUT_MODE == "cdp":
            bg.click_at(self.cdp, info["vx"], info["vy"])
        else:
            click_at(info["x"], info["y"])
        _sleep(1.2, 2.0)
        return self.input_box() is not None

    def input_box(self) -> dict | None:
        find = " || ".join(f"document.querySelector({s!r})" for s in self.INPUT_SELECTORS)
        return element_screen_center(self.cdp, find)

    def input_text(self) -> str:
        try:
            return self.cdp.evaluate_json(
                "const el = document.querySelector(\"%s\");"
                "if (!el) return '';"
                "return (el.value !== undefined && el.value !== null) ? el.value : (el.innerText || '');"
                % ", ".join(self.INPUT_SELECTORS)) or ""
        except CdpError:
            return ""

    def last_messages(self, n: int = 3) -> list[str]:
        try:
            return self.cdp.evaluate_json(
                "const sels = ['[class*=\"message-item\"]', '.chat-message',"
                "              '[class*=\"msg-item\"]'];"
                "for (const s of sels) { const ns = document.querySelectorAll(s);"
                "  if (ns.length > 1) return [...ns].slice(-%d)"
                "     .map(x => (x.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 100)); }"
                "return [];" % n) or []
        except CdpError:
            return []

    def send(self, text: str) -> tuple[bool, str]:
        """输入并发送。返回 (是否成功, 说明)。

        输入框是 `div.chat-input[contenteditable]`，页面提示
        「按 Enter 键发送，按 Ctrl+Enter 键换行发送」→ 所以用 Enter 发送。

        2026-10-17 补发实战结论（探针 _probe_scroll_fix.py 实测）：
          · Enter 确实能发，但 BOSS 清输入框/落消息有 ~3-5 秒延迟
            （请求在飞），等 2.5 秒就下结论会误判「没发出去」，
            紧接着的重试 Enter 还可能造成重复发送 → 改为轮询等待。
          · 之前 13 家「发送后输入框仍有内容」的真因是
            `_click_input_box` 用错了坐标体系（屏幕坐标当视口坐标点），
            见那个方法的注释。
        """
        if not element_exists(self.cdp, self._js_find(self.INPUT_SELECTORS)):
            return False, "找不到聊天输入框（会话没点开？）"
        if not focus_this(self.cdp):
            return False, "无法把 BOSS 窗口切到前台（真实键鼠事件要求窗口在前台）"

        before = self.last_messages(6)
        find = self._js_find(self.INPUT_SELECTORS)
        # 先真实点击输入框：让交互更像真人（风控向），也顺带确保焦点。
        # 注意：光有点击不够 —— 窗口不在前台时 CDP 键盘事件会被页面丢弃，
        # 真正负责「发出去」的是 press_enter_key 里的 JS 合成 Enter 事件。
        self._click_input_box()
        if not type_into_field(self.cdp, find, text):
            return False, "点击/输入聊天输入框失败"
        _sleep(0.4, 1.0)
        press_enter_key(self.cdp, find)

        ok, note = self._wait_sent(text, before)
        if ok:
            return True, note
        # 整轮等完还是老样子 → 焦点大概真丢了，补点一次输入框再按一次 Enter。
        # 能走到这说明「输入框没清空 + 会话里没有这条消息」，重按不会重复发送。
        self._click_input_box()
        press_enter_key(self.cdp, find)
        ok, note = self._wait_sent(text, before)
        if ok:
            return True, note + "（重试后）"
        return False, "发送后输入框仍有内容，可能未发出"

    def _wait_sent(self, text: str, before: list[str],
                   timeout: float = 8.0) -> tuple[bool, str]:
        """轮询等待发送结果，两个成功信号（任一命中即成功）：

        1) 输入框被清空 —— BOSS 发送成功的常规表现；
        2) 会话里出现本条消息的前几个字 —— 消息已落，清空只是渲染慢。

        为什么必须等：2026-10-17 探针实测 Enter 后 2.5 秒输入框还是满的，
        要到 4-5 秒后才清空并出现「送达」。等太短就重试 Enter → 重复发送。
        """
        probe = text[:12]
        already = any(probe in m for m in before)   # 之前误判失败时可能已发过
        deadline = time.time() + timeout
        grew = False
        while time.time() < deadline:
            _sleep(0.7, 1.1)
            after = self.last_messages(6)
            if not already and any(probe in m for m in after):
                grew = True
            if not self.input_text().strip():
                if grew and after:
                    return True, f"已发送（会话里已出现：{after[-1][:40]}…）"
                return True, "已发送（输入框已清空）"
        if grew:
            return True, "已发送（消息已出现在会话里，输入框清除稍慢）"
        return False, ""

    def _click_input_box(self) -> bool:
        """真实点击聊天输入框（坐标有效才点，负坐标说明元素在视口外）。

        ⚠️ 必须用 `bg.viewport_point`（视口坐标），不能用 `input_box()`：
        `input_box()` 走 `element_screen_center`，算的是**屏幕坐标**
        （`window.screenX + 窗口边框 + 元素 left`，还会乘 dpr），
        而 `bg.click_at` 的 `Input.dispatchMouseEvent` 要的是**视口坐标**。
        2026-10-17 实测：这个 Edge 窗口 screenX 异常，
        `input_box()` 返回 x=-610，而同一元素的视口坐标是 (982, 670)
        —— 坐标体系弄错，点击就永远打到视口外面，Enter 也跟着发不出去。
        """
        pt = bg.viewport_point(self.cdp, self._js_find(self.INPUT_SELECTORS))
        if not pt:
            return False
        x, y = pt.get("x", -1), pt.get("y", -1)
        if x is None or y is None or x <= 0 or y <= 0:
            return False
        return bg.click_at(self.cdp, x, y, settle=(0.4, 0.9))
