"""空跑选岗：按当前 config.yaml 扫一遍搜索词，只做过滤判定，不点任何按钮。

用途：**投递前验证「过滤规则有没有把岗位选对」**。只有 bossgreet run --send 才会真发，
     所以每次改完 config 的方向/关键词，先跑这个看清楚命中与跳过，再实投。

用法：
    python scripts/dryscan.py            # 每个搜索词扫 2 页（默认，快）
    python scripts/dryscan.py --pages 4  # 扫 4 页（看得更全，慢）
    python scripts/dryscan.py --only 嵌入式实习        # 只扫指定搜索词
    python scripts/dryscan.py --grep 硬件实习生        # 只打印标题含该词的条目

怎么读输出：
  [命中] … └ 命中：嵌入式,硬件｜性质：实习      → 会真投
  [跳过] … └ 无匹配关键词                     → 多半是 keywords 配窄了（误杀信号）
  [跳过] … └ 非实习/校招岗（标签：1-3年/本科）  → entry_keywords 在按预期工作
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml                                    # noqa: E402
from bossgreet.filters.filter import matches            # noqa: E402
from bossgreet.core.browser import BrowserSession        # noqa: E402
from bossgreet.pages.zhipin import SearchPage            # noqa: E402

# 浏览器窗口不抢前台，但被最小化时 CDP 事件会被丢弃 —— 这里只读 DOM，不受影响


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=2, help="每个搜索词扫几页（默认 2）")
    ap.add_argument("--only", default="", help="只扫这个搜索词")
    ap.add_argument("--grep", default="", help="只打印标题含该词的条目")
    ap.add_argument("--city", default="", help="覆盖 config.filter.city（多城市空跑用）")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    # 与 bossgreet CLI 共用城市解析：--city 支持城市组名（珠三角）/ 逗号列表
    from main import _resolve_cities
    raw_city = args.city or cfg.get("filter", {}).get("city", "")
    cities_allow, search_cities = _resolve_cities(raw_city, cfg)
    cfg["filter"]["cities"] = cities_allow
    city = search_cities[0] if search_cities else ""
    if len(search_cities) > 1:
        print(f"[i] --city {raw_city} 展开为 {len(search_cities)} 个搜索城市，"
              f"空跑只扫第一个（{city}）；过滤按整个集合 {'、'.join(cities_allow)} 判")
    queries = cfg["search_queries"]
    if args.only:
        queries = [q for q in queries if args.only in q] or [args.only]

    s = BrowserSession(cfg["browser"]["cdp_url"])
    s.connect()
    sp = SearchPage(s.page)

    hit_total, skip_total, seen = 0, 0, set()
    for q in queries:
        for page in range(1, args.pages + 1):
            if not sp.open_search(city, q, page=page):
                print(f"  [!] 「{q}」第 {page} 页打开失败")
                break
            time.sleep(2.2)
            sp.wait_for_results(8000)
            cards = sp.job_cards()
            if not cards:
                break
            shown = False
            for job in cards:
                title = job.get("title") or ""
                if not title:
                    continue
                key = (title, job.get("company") or "")
                if key in seen:          # 跨页/跨词会重复推同一条
                    continue
                seen.add(key)
                if args.grep and args.grep not in title:
                    continue
                if not shown:
                    print(f"\n===== 「{q}」第 {page} 页（{len(cards)} 张卡片）=====")
                    shown = True
                ok, reason = matches(job, cfg)
                tags = "/".join(job.get("tags") or [])
                line = f"  {title}  [{tags}]  @{job.get('company')}"
                if ok:
                    hit_total += 1
                    print(f"[命中] {line}\n         └ {reason}")
                else:
                    skip_total += 1
                    print(f"[跳过] {line}\n         └ {reason}")

    print(f"\n===== 空跑汇总（已按 岗位+公司 去重）：命中 {hit_total} / 跳过 {skip_total} =====")
    print("提示：命中数就是这批「可投家数」的上限；跳过里若出现明显对口的岗，"
          "看它的理由是不是「无匹配关键词」——那是 keywords 配窄了。")
    s.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
