# -*- coding: utf-8 -*-
"""把 BOSS 页拉起来、把窗口推到前台，并报告当前状态。正式跑之前用它做交接。

用法：
  python scripts/go_boss.py                 # 报告状态 + 把 BOSS 窗口推到前台
  python scripts/go_boss.py --open          # 顺带打开 BOSS 职位页（城市取 config）
  python scripts/go_boss.py --open --city 东莞 --query 嵌入式
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bossgreet.core.cdp import Cdp                                       # noqa: E402
from bossgreet.core.config import load_config                            # noqa: E402
from bossgreet.core.human import focus_window, enum_browser_windows      # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true", help="打开 BOSS 职位页")
    ap.add_argument("--city", default="", help="--open 时的城市（默认取 config）")
    ap.add_argument("--query", default="")
    args = ap.parse_args()

    cfg = load_config()
    base = cfg["browser"]["cdp_url"]

    c = Cdp(base)
    try:
        v = c.version()
        print(f"[浏览器] {v.get('Browser')}")
    except Exception as e:
        print(f"[!] 连不上 {base}：{e}")
        print('    启动：在项目目录运行 powershell -File scripts/start_browser.ps1')
        sys.exit(1)

    if args.open:
        city = args.city or cfg.get("filter", {}).get("city") or ""
        cmd = [sys.executable, str(Path(__file__).resolve().parent / "open_boss.py")]
        if city:
            cmd += ["--city", city]
        if args.query:
            cmd += ["--query", args.query]
        print(f"[*] 调用 open_boss.py：{city or '默认城市'} {args.query}")
        subprocess.run(cmd)

    t = c.find_target("zhipin.com")
    if t is None:
        print("[!] 调试浏览器里还没有 BOSS 页。加 --open 自动打开：")
        print("    python scripts/go_boss.py --open")
        return

    print(f"[BOSS 页] {(t.get('title') or '')[:60]}")
    print(f"[URL]     {(t.get('url') or '')[:95]}")

    c.attach(t)
    try:
        sal = c.evaluate_json(r"""
const s = document.querySelectorAll('.job-salary');
if (!s.length) return null;
let e = 0; s.forEach(n => { if (!(n.innerText || '').trim()) e++; });
return {total: s.length, empty: e};
""")
        if sal and sal["empty"] == sal["total"]:
            print("[登录态] ✗ 未登录（薪资栏全为空）→ 请在下面这个浏览器窗口里登录 BOSS")
        elif sal:
            print("[登录态] ✓ 已登录（薪资栏有内容）")
        else:
            print("[登录态] ? 当前页没有薪资栏，无法判断")
    except Exception as e:
        print(f"[登录态] 检测失败：{e}")

    ok = focus_window(title_hint=c.title())
    print(f"[窗口] 前置结果：{'✅ 成功' if ok else '❌ 失败（请手动点一下该窗口）'}")
    import ctypes
    fg = ctypes.windll.user32.GetForegroundWindow()
    print("[窗口列表]")
    for w in enum_browser_windows():
        mark = "   <<< 前台" if w["hwnd"] == fg else ""
        print(f"    [{w['exe']:11s}] {w['title'][:58]}{mark}")

    c.close()
    print("\n下一步：运行 scripts/doctor.py，然后 bossgreet preview 看话术。")


if __name__ == "__main__":
    main()
