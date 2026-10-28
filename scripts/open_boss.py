# -*- coding: utf-8 -*-
"""在【已经带调试端口运行的那个浏览器】里打开 BOSS 职位页。

为什么要专门写一个：
  1. 新建标签页用 CDP/Playwright 创建，导航会被 Edge 的标签页休眠机制还原
     （实测：goto zhipin 后 3 秒变回 about:blank / 原 URL）。
     由浏览器自身发起的导航（真实新标签）才能稳稳留住。
  2. 启动浏览器时必须复用【同一个 user-data-dir】，否则登录态不在一个 profile 里，
     而且 9222 端口已被占用，新实例根本拿不到调试端口 —— 静默失败。
     使用 scripts/start_browser.ps1 创建的本机专用配置目录。

用法：
  python scripts/open_boss.py                 # 打开 BOSS 职位页（城市取 config）
  python scripts/open_boss.py --city 东莞
  python scripts/open_boss.py --query 嵌入式
  python scripts/open_boss.py --list          # 只看目标实例信息，不打开
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bossgreet.core.config import load_config   # noqa: E402
from bossgreet.cities import CITY_CODES          # noqa: E402

PS = ("Get-CimInstance Win32_Process | "
      "Where-Object { $_.CommandLine -like '*--remote-debugging-port*' } | "
      "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress")


def find_debug_browser() -> list[dict]:
    """找出所有带 --remote-debugging-port 的浏览器主进程。"""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", PS],
                             capture_output=True, text=True, timeout=60)
        raw = (out.stdout or "").strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
    except Exception as e:
        print(f"[!] 枚举进程失败: {e}")
        return []

    out_list = []
    for item in data:
        cl = item.get("CommandLine") or ""
        name = (item.get("Name") or "").lower()
        if name not in ("msedge.exe", "chrome.exe"):
            continue
        m_port = re.search(r"--remote-debugging-port=(\d+)", cl)
        m_udd = re.search(r'--user-data-dir="?([^"]+?)"?(?:\s|$)', cl)
        m_exe = re.match(r'"?([^"]+\.exe)"?', cl.strip())
        out_list.append({
            "pid": item.get("ProcessId"),
            "name": name,
            "port": m_port.group(1) if m_port else "",
            "udd": m_udd.group(1) if m_udd else "",
            "exe": m_exe.group(1) if m_exe else "",
            "cmd": cl,
        })
    return out_list


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="", help="城市（默认取 config 的 filter.city）")
    ap.add_argument("--query", default="", help="搜索词")
    ap.add_argument("--port", default="", help="CDP 端口（默认取配置 browser.cdp_url）")
    ap.add_argument("--list", action="store_true", help="只列出目标实例，不打开")
    ap.add_argument("--open", action="store_true",
                    help="（默认行为，显式写出便于阅读；本脚本的作用就是打开 BOSS 页）")
    args = ap.parse_args()

    cfg = load_config()
    args.port = args.port or str(urllib.parse.urlparse(cfg["browser"]["cdp_url"]).port or 9222)

    # 已经有 BOSS 页就别再开一个（避免用户浏览器里堆一堆标签）
    if not args.list:
        try:
            from bossgreet.core.cdp import Cdp
            c = Cdp(f"http://127.0.0.1:{args.port}")
            existed = c.find_target("zhipin.com")
            c.close()
            if existed:
                print("[i] 已经有一个 BOSS 页。")
                print("    直接跑 scripts/go_boss.py 或 scripts/doctor.py 即可。")
                return
        except Exception:
            pass

    procs = find_debug_browser()
    if not procs:
        print("[!] 没找到任何带 --remote-debugging-port 的浏览器进程。")
        print('    请先运行 scripts/start_browser.ps1（或手动带 --user-data-dir 启动）。')
        sys.exit(1)

    print("=== 带调试端口运行的浏览器实例 ===")
    for p in procs:
        print(f"  [{p['name']}] pid={p['pid']} port={p['port']}")

    target = next((p for p in procs if p["port"] == args.port), None)
    if target is None:
        print(f"\n[!] 没有实例监听端口 {args.port}")
        sys.exit(1)
    if not target["exe"] or not target["udd"]:
        print("\n[!] 无法解析该实例的 exe / user-data-dir，无法安全复用。")
        sys.exit(1)

    if args.list:
        return

    city = args.city or cfg.get("filter", {}).get("city") or ""
    code = CITY_CODES.get(city, "")
    url = "https://www.zhipin.com/web/geek/jobs"
    qs = []
    if code:
        qs.append(f"city={code}")
    if args.query:
        qs.append("query=" + urllib.parse.quote(args.query))
    if qs:
        url += "?" + "&".join(qs)

    print(f"\n[*] 复用同一 profile 打开：{url}")
    print(f"    用同一个 user-data-dir 启动 → 会并入已运行的那个实例（新开标签页），")
    print(f"    不会抢占端口，也不会丢登录态。")

    # 用同一 user-data-dir + 同一端口启动 → Chromium 会转交给已运行实例。
    # SW_SHOWNOACTIVATE(4)：窗口显示出来但不激活 —— 不会把你的前台窗口抢走
    # （2026-10-18：用户要求「程序不能出现在前端」）。
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 4
    subprocess.Popen([target["exe"],
                      f"--remote-debugging-port={target['port']}",
                      f"--user-data-dir={target['udd']}",
                      url],
                     startupinfo=si)
    print("\n[✓] 已发出打开请求。请在弹出的标签页里登录 BOSS（一次即可，之后长期有效）。")
    print("    登录后运行： python scripts/go_boss.py")


if __name__ == "__main__":
    main()
