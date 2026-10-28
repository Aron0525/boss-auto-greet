# ============ 探针 4：为什么「立即沟通」按钮定位失败 ============
# 现象：greet-only 实跑时 5 家全部报 "点「立即沟通」失败"，但 JD 都读到了
#       （说明详情面板正常，问题出在按钮查找/坐标这一步）。
# 本探针把 click_chat_button 内部的两个中间量分别打出来：
#   ① _js_find_visible(CHAT_SELECTORS) 求值出的是什么元素（还是 null）
#   ② element_screen_center(find) 返回什么
#   ③ 逐个 CHAT_SELECTORS 单独统计命中数与可见性
#
# 用法：python scripts/btn_probe.py
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bossgreet.core.cdp import Cdp                             # noqa: E402
from bossgreet.core import cdpinput as bg                     # noqa: E402
from bossgreet.pages.zhipin import (                          # noqa: E402
    SearchPage, JobDetailPage, element_screen_center, set_input_mode,
)

set_input_mode("cdp")


def main():
    cdp = Cdp("http://127.0.0.1:9222")
    t = cdp.find_target("zhipin.com")
    if not t:
        print("[!] 没找到 BOSS 页面")
        return 1
    cdp.attach(t)
    cdp.call("Page.navigate", {"url": SearchPage.build_search_url("深圳", "嵌入式")})
    for _ in range(20):
        time.sleep(0.8)
        try:
            if int(cdp.evaluate(
                    f"document.querySelectorAll('{SearchPage.CARD_SEL}').length") or 0):
                break
        except Exception as e:
            print(f"[!] {e}")
            return 1

    sp = SearchPage(cdp)
    print(f"[*] 视口环境：{bg.window_visibility(cdp)}")
    cards = sp.job_cards()
    print(f"[*] 卡片 {len(cards)} 张，点开第 2 张…")
    ok = sp.open_card(cards[1])
    print(f"[*] open_card = {ok}   详情面板岗位名 = {sp.detail_name()!r}")
    time.sleep(1.0)

    dp = JobDetailPage(cdp)

    print("\n--- ① 逐个选择器：命中数 / 其中可见数 / 首个可见元素的 rect ---")
    for s in dp.CHAT_SELECTORS:
        r = cdp.evaluate_json(
            "const s = %r;"
            "const ns = [...document.querySelectorAll(s)];"
            "const vis = e => { const b = e.getBoundingClientRect();"
            "                   return b.width > 8 && b.height > 8; };"
            "const v = ns.filter(vis);"
            "const f = v[0];"
            "return {n: ns.length, vis: v.length,"
            "        tag: f ? (f.tagName + '.' + String(f.className || '').slice(0, 30)) : '',"
            "        rect: f ? [Math.round(f.getBoundingClientRect().width),"
            "                   Math.round(f.getBoundingClientRect().height)] : [],"
            "        text: f ? (f.innerText || '').trim().slice(0, 20) : ''};" % s)
        print(f"  {s:<28} 命中={r.get('n')}  可见={r.get('vis')}  "
              f"{r.get('tag','')} {r.get('rect','')} {r.get('text','')!r}")

    find = dp._js_find_visible(dp.CHAT_SELECTORS)
    print("\n--- ② _js_find_visible(...) 求值结果 ---")
    r = cdp.evaluate_json(
        "const el = (%s);"
        "if (!el) return {found: false};"
        "const b = el.getBoundingClientRect();"
        "return {found: true, tag: el.tagName + '.' + String(el.className||'').slice(0,40),"
        "        rect: [Math.round(b.width), Math.round(b.height)],"
        "        text: (el.innerText||'').trim().slice(0,20)};" % find)
    print(f"  {r}")

    print("\n--- ③ element_screen_center(find) ---")
    sc = element_screen_center(cdp, find)
    print(f"  {sc}")

    print("\n--- ④ bg.viewport_point(find) ---")
    print(f"  {bg.viewport_point(cdp, find)}")

    print("\n--- ⑤ 整页里「立即沟通」字样的元素（兜底路径用）---")
    r = cdp.evaluate_json(
        "const out = [];"
        "const vis = e => { const b = e.getBoundingClientRect();"
        "                   return b.width > 8 && b.height > 8; };"
        "document.querySelectorAll('a,button,span,div').forEach(e => {"
        "  if ((e.innerText||'').trim() === '立即沟通' && vis(e)) {"
        "    const b = e.getBoundingClientRect();"
        "    out.push({tag: e.tagName + '.' + String(e.className||'').slice(0,30),"
        "              wh: [Math.round(b.width), Math.round(b.height)],"
        "              vp: [Math.round(b.left + b.width/2), Math.round(b.top + b.height/2)]});"
        "  }});"
        "return out.slice(0, 6);")
    for x in (r or []):
        print(f"  {x}")

    cdp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
