# ============ 实时监控：看一眼脚本是不是真的在按预期操作页面 ============
# 为什么要它：
#   CDP 后台输入【不移动真实鼠标】、也没有光标轨迹 —— 光看屏幕会觉得"它没动"。
#   这个脚本独立采样页面状态，把每一步的客观证据打出来。
#
# 用法（另开一个终端，和 bossgreet CLI 同时跑）：
#   python scripts/watch.py
#   python scripts/watch.py --interval 1.0     # 采样间隔（秒）
#
# 状态只在「发生变化」时打印，避免刷屏；每 30 秒补一条心跳。
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bossgreet.core.cdp import Cdp, CdpError                       # noqa: E402
from bossgreet.pages.zhipin import SearchPage, JobDetailPage, _norm  # noqa: E402

# 一次采样拿全所有关心的量（只读，无副作用）
SAMPLE_JS = r"""
const q = (s) => document.querySelector(s);
const vis = (e) => { if (!e) return false; const b = e.getBoundingClientRect();
                     return b.width > 8 && b.height > 8; };
const cards = document.querySelectorAll('%s');

// 详情面板里的岗位名
let detail = '';
for (const s of ['.job-detail-info .name', '.job-detail-box .job-name',
                 '.job-detail-container h1', '[class*="job-detail"] [class*="name"]']) {
  const n = q(s); if (n && (n.innerText || '').trim()) { detail = n.innerText.trim(); break; }
}

// 「立即沟通」按钮
let btn = '', btnText = '';
for (const s of %s) {
  const n = q(s);
  if (vis(n)) { const b = n.getBoundingClientRect();
                btn = Math.round(b.left + b.width/2) + ',' + Math.round(b.top + b.height/2);
                btnText = (n.innerText || '').trim(); break; }
}
if (!btn) {
  for (const e of document.querySelectorAll('a,button,span,div')) {
    if ((e.innerText || '').trim() === '立即沟通' && vis(e)) {
      const b = e.getBoundingClientRect();
      btn = Math.round(b.left + b.width/2) + ',' + Math.round(b.top + b.height/2);
      btnText = '立即沟通'; break;
    }
  }
}

// 打招呼成功弹窗
const dlg = q('.greet-boss-dialog');
let dlgText = '';
if (dlg) dlgText = (dlg.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 50);

// 聊天输入框
let input = false;
for (const s of ['#chat-input', '.chat-input', 'div.chat-input', "[contenteditable='true']"]) {
  if (vis(q(s))) { input = true; break; }
}

const convs = document.querySelectorAll('.user-list li').length;

return {
  href: location.href,
  visibility: document.visibilityState,
  cards: cards.length,
  detail: detail,
  btn: btn, btnText: btnText, btnCount: (btn ? 1 : 0),
  dlgOpen: !!dlg, dlgText: dlgText,
  chatInput: input, convs: convs,
};
""" % (SearchPage.CARD_SEL,
       "[" + ", ".join(repr(s) for s in JobDetailPage.CHAT_SELECTORS) + "]")


def page_of(href: str) -> str:
    if "/web/geek/jobs" in href:
        return "搜索页"
    if "/web/geek/chat" in href:
        return "聊天页"
    if "/job_detail/" in href:
        return "详情页"
    if "notify-set" in href:
        return "设置页"
    return "其它"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=1.5)
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--for", dest="duration", type=float, default=0,
                    help="只监控这么多秒后自动退出（0=一直跑）")
    args = ap.parse_args()

    cdp = Cdp(f"http://127.0.0.1:{args.port}")
    t = cdp.find_target("zhipin.com")
    if not t:
        print("[!] 没找到 BOSS 页面。先 scripts/open_boss.py --open")
        return 1
    cdp.attach(t)
    print("=" * 78)
    print("  BOSS 执行监控 —— 只在状态变化时打印；Ctrl+C 退出")
    print("  字段：页面 | 卡片数 | 详情面板岗位 | 立即沟通按钮 | 打招呼弹窗 | 聊天框")
    print("=" * 78)

    last = None
    last_beat = time.time()
    deadline = (time.time() + args.duration) if args.duration else None
    try:
        while True:
            if deadline and time.time() > deadline:
                print(f"[{datetime.now():%H:%M:%S}] [*] 监控时长到，退出。")
                break
            try:
                s = cdp.evaluate("(() => {" + SAMPLE_JS + "})()")
            except CdpError as e:
                print(f"[{datetime.now():%H:%M:%S}] [!] 读页面失败：{e}")
                time.sleep(args.interval)
                continue
            if not s:
                time.sleep(args.interval)
                continue

            key = (s.get("href"), s.get("cards"), _norm(s.get("detail", "")),
                   s.get("btnText"), s.get("dlgOpen"), s.get("chatInput"))
            now = time.time()
            if key != last or (now - last_beat) > 30:
                ts = f"{datetime.now():%H:%M:%S}"
                print(f"[{ts}] {page_of(s['href'])}"
                      f" | 卡片={s['cards']}"
                      f" | 详情={s['detail'][:18] or '—'}"
                      f" | 立即沟通={('@' + s['btn']) if s['btn'] else '无'}"
                      f" | 打招呼弹窗={'开：' + s['dlgText'] if s['dlgOpen'] else '无'}"
                      f" | 聊天框={'有' if s['chatInput'] else '无'}"
                      + (f" | 会话={s['convs']}" if s["convs"] else "")
                      + ("" if s["visibility"] == "visible" else "  ⚠窗口不可见(输入会被丢弃)"))
                last = key
                last_beat = now
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[*] 监控已停止。")
    finally:
        cdp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
