# -*- coding: utf-8 -*-
"""干跑：对【线上真实岗位】验证「读卡片 → 过滤 → 读 JD → 生成定制消息」全链路，不发送。

这是正式海投前的最后一道自检。全程走裸 CDP + 真实 OS 键鼠：
  - 读：core.cdp（只 Runtime.evaluate，不触发 BOSS 反调试）
  - 点：core.human（真实键鼠事件）

用法：
  python scripts/dryrun.py                        # 使用 config 第一个搜索词，读 8 个岗位
  python scripts/dryrun.py --city 东莞 --query 嵌入式 --limit 12
  python scripts/dryrun.py --no-jd                # 不点开详情，只验证卡片/过滤/消息
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bossgreet.core.browser import BrowserSession                       # noqa: E402
from bossgreet.core.config import load_config, load_profile, load_templates   # noqa: E402
from bossgreet.filters.filter import matches                           # noqa: E402
from bossgreet.messaging.providers import make_provider                   # noqa: E402
from bossgreet.pages.zhipin import SearchPage, JobDetailPage, focus_this  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="")
    ap.add_argument("--query", default="")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--no-jd", action="store_true", help="不点开详情读 JD")
    args = ap.parse_args()

    cfg = load_config()
    profile = load_profile()
    templates = load_templates()
    city = args.city or cfg.get("filter", {}).get("city") or ""
    query = args.query or (cfg.get("search_queries") or ["嵌入式"])[0]

    provider = make_provider(cfg, profile, templates, messages_path=None)
    print(f"[*] 生成器：{provider.describe()}")
    print(f"[*] 目标：城市={city or '不限'} 关键词={query}")

    session = BrowserSession(cfg["browser"]["cdp_url"])
    try:
        cdp = session.connect()
    except Exception as e:
        print(f"[!] 连接失败：{e}")
        print("    先运行： python scripts/open_boss.py --open")
        return
    print(f"[*] 已附着 {session.browser_name()}")

    search = SearchPage(cdp)
    print(f"[*] 当前页：{search.title()[:50]}")

    if not search.open_search(city, query):
        print("[!] 没拿到搜索结果。可能未登录 / 需要安全校验 / 页面被切到后台。")
        print("    先跑 doctor：python scripts/doctor.py")
        session.disconnect()
        return
    print(f"[*] 已到：{search.title()[:46]}")
    print(f"[*] URL ：{search.url()[:96]}")

    cards = search.job_cards()
    n = len(cards)
    print(f"\n[*] 本页卡片数：{n}（只取前 {min(n, args.limit)} 个）\n")
    print("=" * 78)

    kept = filtered = 0
    for job in cards[: args.limit]:
        ok, reason = matches(job, cfg)
        mark = "保留" if ok else "过滤"
        kept += 1 if ok else 0
        filtered += 0 if ok else 1
        print(f"[{job['index']}] [{mark}] {job['title']} | {job.get('salary') or '-'} | "
              f"{job.get('company') or '-'} | {job.get('city') or '-'}")
        print(f"      标签={job.get('tags')} | 坐标=({job['x']},{job['y']})")
        if not ok:
            print(f"      理由：{reason}")
            continue

        jd = ""
        if not args.no_jd:
            search.open_card(job)
            time.sleep(1.2)
            detail = JobDetailPage(cdp)
            jd = detail.read_jd_text()
            if jd:
                print(f"      JD：读到 {len(jd)} 字 —— {jd[:70].replace(chr(10), ' ')}...")
            else:
                print("      JD：没读到（选择器需按实际页面结构调整）")
            if detail.chat_button() is not None:
                print("      ✓ 找到「立即沟通」按钮")
            else:
                print("      ✗ 没找到「立即沟通」按钮（可能未登录）")

        msg = provider.compose({**job, "jd_text": jd})
        print(f"      → 消息({len(msg.text)}字, 族={msg.family})")
        for line in _wrap(msg.text, 66):
            print(f"        {line}")
        if msg.warnings:
            print(f"        ⚠ {msg.warnings}")
        print("-" * 78)

    print("\n" + "=" * 78)
    print(f"[结果] 保留 {kept} / 过滤 {filtered} / 共读 {kept + filtered} 个岗位")
    if kept == 0:
        print("      保留数为 0 → 检查 config.yaml 的 keywords / exclude_keywords。")
    session.disconnect()


def _wrap(text: str, width: int) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= width:
            lines.append(cur)
            cur = ""
    if cur:
        lines.append(cur)
    return lines


if __name__ == "__main__":
    main()
