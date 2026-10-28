# ============ 探针 5：挖出「已向BOSS发送消息」弹窗的真实结构 ============
# 已知：截图显示弹窗按钮是「留在此页」+「继续沟通」，右上角有 ×。
# 旧代码用 includes('留在此页') 在 a,button,span,div 里找，会先命中外层 div 容器
# → 点了个寂寞。本探针把弹窗内部所有元素的层级/尺寸/文本/坐标打出来，定出该点谁。
#
# 用法：先把弹窗留着（或让脚本跑一次），然后 python scripts/dialog_probe.py
#   加 --greet 会先自动点一次「立即沟通」把弹窗弄出来。
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bossgreet.core.cdp import Cdp                                 # noqa: E402
from bossgreet.core import cdpinput as bg                          # noqa: E402
from bossgreet.pages.zhipin import (                               # noqa: E402
    SearchPage, JobDetailPage, set_input_mode,
)

set_input_mode("cdp")

DUMP_JS = r"""
const dlg = document.querySelector('.greet-boss-dialog');
if (!dlg) return {open: false};

// 递归列出弹窗里每个元素的层级、标签、类、尺寸、视口坐标、文本
const out = [];
const walk = (el, depth) => {
  const b = el.getBoundingClientRect();
  const txt = (el.innerText || '').replace(/\s+/g, ' ').trim();
  out.push({
    depth: depth,
    tag: el.tagName,
    cls: String(el.className || '').slice(0, 46),
    ka: el.getAttribute('ka') || '',
    w: Math.round(b.width), h: Math.round(b.height),
    vp: [Math.round(b.left + b.width / 2), Math.round(b.top + b.height / 2)],
    area: Math.round(b.width * b.height),
    text: txt.slice(0, 34),
    kids: el.children.length,
  });
  for (const c of el.children) walk(c, depth + 1);
};
walk(dlg, 0);

// 顺便看看弹窗外层容器类名（有时点外层也能关）
const outer = dlg.parentElement
  ? dlg.parentElement.tagName + '.' + String(dlg.parentElement.className || '').slice(0, 40)
  : '';
return {open: true, outer: outer, nodes: out};
"""


def main():
    greet = "--greet" in sys.argv
    cdp = Cdp("http://127.0.0.1:9222")
    t = cdp.find_target("zhipin.com")
    if not t:
        print("[!] 没找到 BOSS 页面")
        return 1
    cdp.attach(t)

    sp = SearchPage(cdp)
    if greet:
        cdp.call("Page.navigate", {"url": SearchPage.build_search_url("深圳", "嵌入式")})
        for _ in range(20):
            time.sleep(0.8)
            if int(cdp.evaluate(
                    f"document.querySelectorAll('{SearchPage.CARD_SEL}').length") or 0) > 0:
                break
        cards = sp.job_cards()
        # 挑一个还没打过招呼的（卡片里没有「继续沟通」字样的那个）
        idx = 0
        for c in cards:
            if "已沟通" not in (c.get("title") or "") and "继续沟通" not in (c.get("title") or ""):
                idx = c.get("index", 0)
                break
        print(f"[*] 打开第 {idx+1} 张卡片并点「立即沟通」…")
        if not sp.open_card(cards[idx]):
            print("[!] open_card 失败")
            return 1
        time.sleep(0.8)
        dp = JobDetailPage(cdp)
        if not dp.click_chat_button():
            print("[!] 点立即沟通失败")
            return 1
        for _ in range(10):
            time.sleep(0.6)
            if dp.greet_dialog_open():
                break

    d = cdp.evaluate("(() => {" + DUMP_JS + "})()") or {}
    print(f"\n[*] 弹窗是否打开：{d.get('open')}   外层容器：{d.get('outer','')}")
    if not d.get("open"):
        print("    （没弹窗 —— 只在浏览器里已有相关弹窗时运行此诊断）")
        return 0

    nodes = d.get("nodes", [])
    print(f"[*] 弹窗内共 {len(nodes)} 个元素，按层级缩进：\n")
    for n in nodes:
        text = n["text"]
        is_btn = text in ("留在此页", "继续沟通")
        mark = "  ★按钮候选" if is_btn else ""
        print(f"{'  ' * min(n['depth'], 8)}{n['tag']}.{n['cls'][:40]:<40}"
              f" {n['w']}x{n['h']} area={n['area']:<7} vp={n['vp']}"
              f" kids={n['kids']} ka={n['ka']!r} text={text!r}{mark}")

    # 给出「最深层/面积最小」的两个候选
    cands = [n for n in nodes if n["text"] in ("留在此页", "继续沟通") and n["w"] > 4 and n["h"] > 4]
    print("\n--- 按钮候选（按面积从小到大，越小越可能是真正的可点元素）---")
    for n in sorted(cands, key=lambda x: x["area"])[:6]:
        print(f"  <{n['tag']} class={n['cls']!r}> {n['w']}x{n['h']} vp={n['vp']} "
              f"text={n['text']!r} ka={n['ka']!r}")

    cdp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
