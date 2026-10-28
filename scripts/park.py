"""把浏览器窗口在「屏幕外 ↔ 屏幕上」之间搬动。

为什么要这个工具：
  bossgreet CLI 默认会把浏览器窗口挪到屏幕外（core/winpos.py），这样它既不出现在
  前台、也吃不到你的鼠标。但你偶尔需要自己看一眼（登录、核对招呼、看验证码）——
  用这个脚本把它搬回来，看完再搬走。

用法：
  python scripts/park.py            # 只看状态（窗口在哪、前台是谁）
  python scripts/park.py --show     # 搬回屏幕（会顺带把它切到前台）
  python scripts/park.py --hide     # 挪回屏幕外（安静，不抢前台）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bossgreet.core.cdp import Cdp, CdpError      # noqa: E402
from bossgreet.core import winpos                 # noqa: E402
from bossgreet.core.config import load_config     # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="浏览器窗口搬到屏幕外 / 搬回来")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--show", action="store_true", help="搬回屏幕并切到前台")
    g.add_argument("--hide", action="store_true", help="挪到屏幕外（默认状态）")
    args = ap.parse_args()

    cfg = load_config()
    base = cfg["browser"]["cdp_url"]
    cdp = Cdp(base)
    try:
        targets = cdp.list_targets()
    except Exception as e:
        print(f"[✗] 连不上浏览器调试端口 {base}：{e}")
        print("    先启动： start_browser.bat")
        sys.exit(1)

    boss = None
    for t in targets:
        if "zhipin.com" in (t.get("url") or ""):
            boss = t
            break
    if not boss:
        print("[✗] 没有找到 BOSS 页面（zhipin.com）。")
        print("    先打开： python scripts/open_boss.py --open")
        sys.exit(1)
    cdp.attach(boss)

    st = winpos.park_status(cdp)
    print(f"[*] 页面：{(cdp.title() or '')[:50]}")
    print(f"[*] 窗口位置：left={st.get('left')} top={st.get('top')} "
          f"{st.get('width')}x{st.get('height')} → "
          f"{'屏幕外（安静）' if st.get('offscreen') else '屏幕上（会挡鼠标）'}")
    print(f"[*] 当前前台窗口：{winpos.foreground_title() or '(读不到)'}")

    if args.show:
        ok = winpos.show(cdp)
        print("[✓] 已搬回屏幕" if ok else "[✗] 搬回来失败")
        print(f"    现在前台窗口：{winpos.foreground_title() or '(读不到)'}")
    elif args.hide:
        ok = winpos.park(cdp)
        print("[✓] 已挪到屏幕外" if ok else "[✗] 挪走失败")
        print(f"    现在前台窗口：{winpos.foreground_title() or '(读不到)'}")
    cdp.close()


if __name__ == "__main__":
    main()
