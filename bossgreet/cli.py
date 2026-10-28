# ============ 控制层：按岗位定制招呼语 + 批次确认海投 ============
# 流程：搜索 → 过滤 → 去重 → 打开详情读 JD → 生成定制消息 → 批次确认 → 发送 → 记录
#
# ⚠️ 发送的真实链路（2026-10-13 实测，和旧版完全不同，务必先读懂）：
#   1. 在搜索结果页点「立即沟通」→ BOSS **自动发出账号里的默认招呼语**
#      （不会弹输入框，而是弹一个「已发送/留在此页继续沟通」的提示框）
#   2. 关掉提示框
#   3. 定制消息**只能在聊天页 `/web/geek/chat` 里发**（列表页没有输入框）
#   4. 在聊天页点开刚打招呼的那个会话 → 输入 → Enter
#   因为第 3 步把标签页导航走了，所以**每发完一条都要重新打开搜索结果页**，
#   再用 contacted 去重接着往下扫 —— 这是本文件循环结构的由来。
#
# 三种生成器（config.yaml 的 message.generator 决定）：
#   template  本地模板+槽位       零成本零延迟，永不失败
#   llm       模型读完整 JD 逐条写  真·逐岗定制，需要 API Key
#   external  读 messages.json    由 Agent/人工预生成，脚本只发送
#
# 用法见 README；直接运行 bossgreet 默认进入只读预览。
from __future__ import annotations

import argparse
from importlib import resources
import json
import os
import random
import sys
import time
from pathlib import Path

from bossgreet.core.browser import BrowserSession, human_delay
from bossgreet.core.launcher import ensure_browser, ensure_jobs_page
from bossgreet.core import cdpinput as bg
from bossgreet.core import winpos
from bossgreet.core.config import (
    load_config, load_profile, load_templates, state_path)
from bossgreet.filters.filter import matches
from bossgreet.filters.dedup import ContactLog, job_key
from bossgreet.messaging.providers import make_provider
from bossgreet.pages.zhipin import (SearchPage, JobDetailPage, ChatPage, focus_this,
                                    set_input_mode, element_exists)
from bossgreet.reporting.export import export_contacted, load_history

# 这两个是纯逻辑，已抽到独立模块（便于不连浏览器也能单测）。
# 这里挂成同名别名，主流程里的调用点就不必逐个改。
from bossgreet.cities import resolve_cities as _resolve_cities          # noqa: E402
from bossgreet.filters.company import (                                # noqa: E402
    normalize_company_key as _company_key,
)


def _ask(msg: str, default: str = "q") -> str:
    """等一次人工输入。

    无人值守（后台任务、stdin 关闭）时 input() 会抛 EOFError —— 此时返回 `default`。
    **default 必须按调用点的语义分别给**：
      - 安全校验 / 登录弹窗 / 不在目标站 这类"环境不对"的提示 → 默认 `q`（退出）。
        宁可少投，也不要在验证页上继续硬点。
      - 发送相关异常 → 默认 `q`（停止，等待人工核对）。
    """
    try:
        return input(msg).strip().lower()
    except EOFError:
        print(f"  [i] 检测不到交互输入，按默认处理：{default}")
        return default


def _limit_reached(cmd: str, attempted: int, sent: int, limit: int) -> bool:
    """运行模式的上限按真实发送计，纯读取模式仍按已处理岗位计。"""
    return sent >= limit if cmd == "run" else attempted >= limit


def _company_hits_from_history(history: list[dict]) -> dict[str, int]:
    """跨重启保留已成功联系过的公司计数；不确定/平台拒绝状态不计入。"""
    hits: dict[str, int] = {}
    for entry in history:
        if entry.get("mode") in {"uncertain", "blocked_profile_mismatch"}:
            continue
        key = _company_key(entry.get("company"))
        if key:
            hits[key] = hits.get(key, 0) + 1
    return hits


# 注：_company_key（公司名归一化）与 _resolve_cities（城市组解析）两项纯逻辑
# 已抽到 bossgreet/filters/company.py 与 bossgreet/cities.py，
# 在 import 区挂了同名别名 —— 这样下面的主流程调用点一个都不用改。

_shot_seq = 0


def _shot(cdp, shots_dir, tag: str):
    """存一张截图，作为「这一步确实执行到了」的可视证据。失败不影响主流程。

    截图走 Page.captureScreenshot —— 不需要 Page.enable，也不会被 BOSS 反调试命中。
    """
    global _shot_seq
    if not shots_dir:
        return
    _shot_seq += 1
    name = f"{_shot_seq:02d}_{tag}.png"
    try:
        ok = cdp.screenshot(str(shots_dir / name))
        print(f"     [图] {name}" + ("" if ok else "  （截图失败）"))
    except Exception as e:
        print(f"     [图] 截图失败：{e}")


def _pipeline(args) -> int:
    """主投递流水线。args 由 main() 解析好；返回进程退出码。

    这一段是「真的会动你的账号」的部分：筛岗位 → 开详情 → 打招呼 → 记账。
    只读的活儿（scan / preview）会复用同一条流水线，只是 send 环节被关掉，
    这样"只读"和"实投"走的是同一套筛选逻辑，不会出现试跑和真跑两码事。
    """
    cfg = load_config(getattr(args, "config", None))
    # 命令行覆盖城市 —— 过滤层（commands.filter.matches）读的是同一个 cfg，
    # 所以必须在这里写回去，否则搜索在深圳、过滤还按 config 里的旧城市判。
    if args.city:
        cfg.setdefault("filter", {})["city"] = args.city
    profile = load_profile(getattr(args, "profile", None))
    templates = load_templates()

    # 输入通道：config 决定，命令行可覆盖
    mode = (cfg.get("browser") or {}).get("input_mode", "cdp")
    if args.os_input:
        mode = "os"
    if args.cdp_input:
        mode = "cdp"
    # 硬闸：os 模式会真的移动鼠标、必须把窗口切到前台 ——
    # 用户要求「程序运行不能占据我鼠标的使用权」，所以只认显式 --os-input。
    if mode == "os" and not args.os_input:
        print("[!] config 里写的是 input_mode: os（真实键鼠 = 会占鼠标），"
              "但这次没加 --os-input → 已强制改用 CDP 后台输入。")
        mode = "cdp"
    active = set_input_mode(mode)

    shots_dir = Path(args.shots) if args.shots else None
    if shots_dir:
        shots_dir.mkdir(parents=True, exist_ok=True)
        print(f"[*] 每步截图 → {shots_dir}（用来确认每一步真的执行到位）")

    # 投递方式：默认读 config，命令行可覆盖
    send_custom = bool((cfg.get("outreach") or {}).get("send_custom_message", False))
    if args.custom:
        send_custom = True
    if args.greet_only:
        send_custom = False

    cfg_max = cfg.get("message", {}).get("max_chars")
    if cfg_max:
        templates.setdefault("settings", {})["max_chars"] = int(cfg_max)

    provider_cfg = cfg if send_custom else {"message": {"generator": "template"}}
    provider = make_provider(provider_cfg, profile, templates,
                             messages_path=args.messages or None)
    if send_custom:
        print(f"[*] 消息生成器：{provider.describe()}")
        if provider.needs_jd:
            print("    （需要读到 JD 正文；读不到时会退化为只按职位名生成）")
        print("[*] 投递方式：打招呼 + 补发定制消息（--custom）")
    else:
        print("[*] 投递方式：只打招呼 —— 点「立即沟通」，用你在 BOSS「个人中心 → "
              "打招呼语」里设置的那条默认招呼语（--greet-only）")
        print("    定制消息的生成/发送代码都还在，改 config 的 outreach.send_custom_message "
              "或加 --custom 就能切回来。")

    capture_path = (Path(args.capture or state_path("jobs", "jobs.jsonl", cfg))
                    if args.cmd == "capture" else None)
    capture_fp = None
    if capture_path:
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        capture_fp = capture_path.open("a", encoding="utf-8")
        print(f"[*] 采集模式：岗位+JD 将追加写入 {capture_path}（不生成、不发送）")

    if send_custom and cfg.get("default_greeting_notice", True):
        print("=" * 72)
        print("提醒：点「立即沟通」时 BOSS 会先自动发一条你账号的默认招呼语，")
        print("      然后本脚本的定制消息才发出去 —— HR 会连续看到两条。")
        print("=" * 72)

    print("[*] 检查配置的浏览器与本机调试端口...")
    try:
        launched_browser = ensure_browser(cfg)
        if launched_browser:
            print("[*] 已打开 BOSS 登录页。请在这个专用浏览器窗口完成登录/安全校验，"
                  "然后重新运行相同命令。")
            print("    本次没有读取岗位、写入联系记录或发送任何消息。")
            return 0
        ensure_jobs_page(cfg)
        session = BrowserSession(cfg["browser"]["cdp_url"])
        cdp = session.connect()
    except Exception as e:
        print(f"[!] 浏览器连接失败：{e}")
        print("    请核对 config.yaml 的 browser.name / cdp_url / executable，"
              "并在专用浏览器窗口手动登录。")
        return 1
    print(f"[*] 已附着 {session.browser_name()}：{(cdp.title() or '')[:50]}")
    if active == "cdp":
        print("[*] 输入通道：CDP 后台输入 —— 不抢前台，Edge 可以一直躲在后台跑。")
        print("    ⚠ 唯一前提：那个浏览器窗口别最小化（被别的窗口完全盖住没关系，")
        print("       页面被盖成 hidden 时程序会自动把它拉回 active，你继续用你的）。")
    else:
        print("[*] 输入通道：真实 OS 键鼠 —— 会占用鼠标并把浏览器窗口切到前台。")

    # ---- 把窗口挪到屏幕外：全程不占屏幕、不抢鼠标（2026-10-18）----
    # 用户硬性要求：「程序运行不能出现在前端，不能占据我鼠标的使用权」。
    # 输入早就是 CDP（不动真实光标），但窗口自己会留在最前面挡住工作区，
    # 于是鼠标点哪儿都先打到它身上。解法不是抢回来，而是让它压根不在屏幕上：
    #   Browser.getWindowForTarget → Browser.setWindowBounds(left=-32000)
    # 窗口到屏幕外后页面会被标成 hidden —— 不影响 CDP，每次点击前
    # ensure_interactive() 会把它拉回 active。实测点击与导航均正常。
    parked = False
    if active == "cdp" and not args.show_window and not launched_browser:
        if (cfg.get("browser") or {}).get("park_window", True):
            fg_before = winpos.foreground_title()
            parked = winpos.park(cdp)
            if parked:
                st = winpos.park_status(cdp)
                print(f"[*] 窗口已挪到屏幕外（left={st.get('left')}）：你看不到它，"
                      f"它也挡不住你的鼠标。")
                fg_after = winpos.foreground_title()
                print(f"    当前前台窗口：{fg_after or '(读不到)'}")
                if fg_before and fg_after and fg_before != fg_after:
                    print(f"    [!] 前台被换成了别的窗口（原来是「{fg_before[:40]}」）")
                print("    想自己看一眼：python scripts/park.py --show")
    elif active == "cdp":
        print("[*] 浏览器窗口保留在屏幕上，方便首次登录或检查预览。")

    # ---- 焦点护卫：Chromium 随时可能把那个「屏幕外窗口」弄到前台 ----
    # 它抢一次，就还你一次（原因见 core/winpos.FocusGuard 的注释）。
    guard = winpos.start_focus_guard() if parked else None

    search = SearchPage(cdp)
    for _ in range(20):
        if search.on_target_site():
            break
        time.sleep(0.5)
    else:
        print("[!] 当前页面未就绪。请在配置的浏览器窗口手动登录并打开 BOSS 职位页后重试。")
        session.disconnect()
        return 1
    if search.is_refreshed():
        print("[!] BOSS 当前要求安全校验。请在专用浏览器窗口手动完成校验和登录，"
              "回到职位页后重新运行预览。")
        session.disconnect()
        return 1
    focus_this(cdp)
    if active == "cdp":
        vis = bg.window_visibility(cdp)
        if vis.get("hidden") or vis.get("visibility") != "visible":
            if not parked:
                print(f"  [!] 页面当前不可见（{vis}）—— 把它拉回 active（不抢前台）。")
            # 2026-10-17 晚实测：页面 hidden（被完全遮挡）时坐标点击被静默丢弃，
            # 「立即沟通」点了没反应、招呼没发出去（两家假成功）。
            # 解法：setWebLifecycleState(active) + setFocusEmulationEnabled，
            # 不抢前台就能让被遮挡的页面恢复可点击（focus_this 里已封装）。
            # 窗口被挪到屏幕外时同样是 hidden —— 走同一条恢复路径即可。
            ok_vis = False
            for _try in range(3):
                if focus_this(cdp):
                    ok_vis = True
                    break
                time.sleep(1.0)
            vis = bg.window_visibility(cdp)
            if parked:
                print(f"[i] 屏幕外窗口的页面状态：{vis.get('visibility')} "
                      f"（已拉回 active，点击/导航正常）")
            else:
                print(f"      恢复后：{vis}")
            if not ok_vis:
                print("[✗] 页面还是拉不回 active。最小化状态下 CDP 鼠标事件会被丢弃。")
                print("    处理：确认那个浏览器窗口【没有最小化】—— 被别的窗口盖住没关系，")
                print("    本程序不抢前台，Edge 可以一直躲在后台，然后重跑。")
                session.disconnect()
                sys.exit(1)

    if search.login_hint():
        print("[!] 检测到【未登录】（薪资栏全为空 —— BOSS 对未登录用户不给薪资）。")
        print("    请在那个浏览器窗口里登录 BOSS（扫码一次即可），然后重跑。")
        session.disconnect()
        sys.exit(1)

    # 上次跑到一半可能把「已向BOSS发送消息」弹窗留在页面上 —— 先关掉。
    # 不关的话它会盖住左侧卡片，第一下点击就打到弹窗上了。
    if JobDetailPage(cdp).greet_dialog_open():
        print("  [i] 页面上还留着上次的「已向BOSS发送消息」弹窗，先关掉")
        JobDetailPage(cdp).dismiss_greet_dialog()
        time.sleep(0.6)
    # 「今日沟通次数」温馨提示同理：它的遮罩会吞掉之后所有坐标点击。
    if JobDetailPage(cdp).chat_block_open():
        print("  [i] 页面上还留着上次的「今日沟通次数」温馨提示，先关掉")
        JobDetailPage(cdp).dismiss_chat_block()
        time.sleep(0.6)

    # ---- 多城市：--city 可传单个城市、逗号/顿号分隔列表、或城市组名（珠三角）----
    raw_city = args.city or cfg.get("filter", {}).get("city", "")
    cities_allow, search_cities = _resolve_cities(raw_city, cfg)
    cfg["filter"]["cities"] = cities_allow
    print(f"[*] 可投城市集合：{'、'.join(cities_allow) or '不限'}")
    print(f"[*] 本批搜索城市：{'、'.join(search_cities) or '不限'}")
    daily_limit = int(cfg["pace"].get("daily_limit", 40))
    if daily_limit < 1:
        raise ValueError("pace.daily_limit 必须是正整数")
    if args.limit < 0:
        raise ValueError("--limit 不能为负数")
    if args.limit:
        daily_limit = min(daily_limit, args.limit)
    batch_size = int(cfg["pace"].get("batch_size", 10))
    if batch_size < 1:
        raise ValueError("pace.batch_size 必须是正整数")
    min_d, max_d = cfg["pace"]["min_delay_s"], cfg["pace"]["max_delay_s"]
    max_pages = int(getattr(args, "pages", 0)
                    or (cfg.get("filter", {}) or {}).get("max_pages", 5))

    # 单公司上限：避免在同一批次反复联系同一家。设 0 = 不限制。
    max_per_company = int(cfg.get("filter", {}).get("max_per_company", 0) or 0)
    company_hits: dict[str, int] = {}

    # 公司黑名单（2026-10-24 加）：有些岗位点「立即沟通」永远不弹窗
    # （大概率是走网申/投递简历的类型），每轮都会被当成新岗位重试一次。
    # 按归一化公司名匹配。
    skip_companies = {_company_key(c) for c in (cfg.get("filter", {}).get("skip_companies") or [])}

    queries = [args.query] if args.query else (cfg.get("search_queries") or [])
    # 记录文件：路径可配（paths.contacted），默认落在当前工作目录
    contact = ContactLog(os.environ.get("BOSSGREET_CONTACTED")
                         or state_path("contacted", "contacted.json", cfg))
    contacted = contact.keys()
    if max_per_company:
        company_hits = _company_hits_from_history(contact.history())
    base_targets = {t.get("id") for t in session.all_targets()}

    # 空跑模式（scan）全程不点卡片，只报命中；--grep 用来只盯某个岗位
    args_scan = bool(getattr(args, "scan", False))
    grep = (getattr(args, "grep", "") or "").strip()

    stats = {"sent": 0, "failed": 0, "skipped": 0, "filtered": 0, "dup": 0,
             "scan_matched": 0}
    attempted = 0
    stop = False
    # 本次运行里「试过但失败」的岗位 —— 发送会把标签页导航走，之后要重开列表页重扫，
    # 没有这个集合的话同一个岗位会被无限重试。
    failed_keys: set[str] = set()
    # 「已沟通过」的岗位（详情页按钮显示「继续沟通」）—— 点了也不会再发招呼，
    # 不重复投；重扫时直接跳过，避免每轮都打印一遍。
    skip_keys: set[str] = set()
    # 已计入统计的 key —— 重开页面会重复扫到同一批卡片，用它防止数字翻倍。
    counted: set[str] = set()

    # 外层城市循环：search_cities 为空/[''] 时就是原来的单城市行为
    for city in search_cities:
        for query in queries:
            if stop or _limit_reached(args.cmd, attempted, stats["sent"], daily_limit):
                break
            print(f"\n{'=' * 72}\n[*] 搜索关键词＝{query}（城市＝{city or '不限'}）")
            if parked:
                # 每换一个关键词再确认一次窗口在屏幕外 —— 万一被 Edge 还原或被拖回来，
                # 这里补一刀（一次 CDP 调用，几乎不耗时）。
                winpos.park(cdp)

            page_no = 1
            # ★ 2026-10-15 改进：BOSS 的岗位列表是【滚动无限加载】（实测 15→30→45→60…），
            #   所以不再「每轮重开搜索页 URL 翻页」——那样既有整页刷新（肉眼可见一直在闪）
            #   又慢得多。现在 page_no 的含义 = 第几次「滚到底加载更多」。
            #   首轮打开搜索页；之后每轮滚到底把新卡片灌进来，接着扫。
            while (page_no <= max_pages and not stop
                   and not _limit_reached(args.cmd, attempted, stats["sent"], daily_limit)):
                if page_no == 1:
                    ok_round = _search_with_retry(search, query, cfg, city, 1)
                else:
                    ok_round = search.load_more_by_scroll()
                    if not ok_round:
                        print(f"  [i] 「{query}」滚到底也没有新岗位了，本词收工")
                        break
                if not ok_round:
                    print(f"[✗] 「{query}」第 {page_no} 轮没拿到结果")
                    stop = True
                    break

                # 内层再套一层循环的原因（这是用户明确要求的行为）：
                #   打完招呼**不刷新页面**，就地重取一份卡片列表，然后点下一个岗位。
                #   重取是必要的 —— 打过招呼的卡片会变成「继续沟通」，列表可能重排；
                #   而刚招呼过的那家在 contacted 里，重扫时会被自动跳过。
                navigated = False       # 本页是否被导航走了（只有模式 B / 安全刷新才会）
                rescan = 0

                while True:
                    cards = search.job_cards()
                    if rescan == 0:
                        print(f"\n----- 「{query}」第 {page_no} 页，共 {len(cards)} 个职位 -----")
                    else:
                        print(f"  [i] 不刷新页面，就地重扫本页（{len(cards)} 张卡片）继续点下一个")
                    rescan += 1

                    greeted = False     # 本趟是否真的打了一次招呼（决定要不要继续重扫）

                    for job in cards:
                        if stop or _limit_reached(args.cmd, attempted, stats["sent"], daily_limit):
                            break
                        if not job.get("title"):
                            continue

                        key = job_key(job)
                        ok, reason = matches(job, cfg)
                        if not ok:
                            if key not in counted:
                                counted.add(key)
                                stats["filtered"] += 1
                                print(f"  [跳过] {job['title']} — {reason}")
                            continue
                        if key in failed_keys:
                            continue
                        if key in skip_keys:
                            continue
                        if key in contacted:
                            if key not in counted:
                                counted.add(key)
                                stats["dup"] += 1
                                print(f"  [已联系过] {job['title']} @ {job.get('company', '')}")
                            continue

                        # ---- 公司黑名单 ----
                        if skip_companies:
                            ck = _company_key(job.get("company"))
                            if ck in skip_companies:
                                stats["filtered"] += 1
                                print(f"  [跳过] {job['title']} — 公司在黑名单"
                                      f"（{job.get('company') or '-'}：招呼发不出去）")
                                continue

                        # ---- 单公司上限 ----
                        if max_per_company:
                            ck = _company_key(job.get("company"))
                            if ck and company_hits.get(ck, 0) >= max_per_company:
                                stats["filtered"] += 1
                                print(f"  [跳过] {job['title']} — 单公司已达上限"
                                      f"（{job.get('company') or '-'} 本次已投 {max_per_company} 个）")
                                continue

                        # ---- 空跑（scan）：走到这里就够了，不点卡片、不开详情 ----
                        # 和正式跑共用同一套筛选规则，所以试跑结论对真跑有效。
                        if args_scan:
                            if not grep or grep in job["title"]:
                                stats["scan_matched"] += 1
                                print(f"  [命中] {job['title']} | "
                                      f"{job.get('salary') or '-'} | "
                                      f"{job.get('company') or '-'}")
                            continue

                        print(f"\n  [匹配] {job['title']} | {job.get('salary') or '-'} | "
                              f"{job.get('company') or '-'} | {job.get('city') or '-'}")

                        # ---- 打开详情 → 读 JD ----
                        # open_card 会自校验详情面板是否真的切到了这个岗位；
                        # 切不动就必须跳过 —— 否则会拿着上一条岗位的 JD 生成消息，静默发错。
                        if not search.open_card(job):
                            print("  [!] 详情面板没切到这个岗位（卡片不可点或已改版），跳过以防发错消息")
                            failed_keys.add(key)
                            if args.cmd == "run":
                                stop = True
                                break
                            continue
                        if search.is_refreshed():
                            print("  [⚠] 触发了 BOSS 安全校验刷新，暂停。")
                            print("      请在浏览器里手动完成验证后回车继续（q 退出）...")
                            if _ask("      > ") == "q":
                                stop = True
                                break
                            navigated = True        # 列表页已被刷掉，重开
                            break

                        work_cdp, is_new_tab = _resolve_detail(session, cdp, base_targets)
                        detail = JobDetailPage(work_cdp)
                        _shot(work_cdp, shots_dir, "1_detail_opened")

                        jd = detail.read_jd_text()
                        if jd:
                            print(f"  [JD] 读到 {len(jd)} 字")
                        elif provider.needs_jd:
                            print("  [!] 没读到 JD 正文，只能按职位名生成")

                        if detail.has_login_wall():
                            print("  [!] 出现登录弹窗 —— 请在该浏览器里登录后回车继续（q 退出）...")
                            if _ask("") == "q":
                                stop = True
                                break
                            jd = detail.read_jd_text()
                            if detail.has_login_wall():
                                print("  [✗] 登录状态仍不可用，已停止。")
                                stop = True
                                break

                        if capture_fp is not None:
                            capture_fp.write(json.dumps({**job, "jd_text": jd},
                                                        ensure_ascii=False) + "\n")
                            capture_fp.flush()
                            print(f"  [采集] 已写入 {capture_path.name}")
                            attempted += 1
                            if is_new_tab:
                                _maybe_close_tab(session, work_cdp, is_new_tab, base_targets)
                            human_delay(min_d * 0.3, max_d * 0.6)
                            continue

                        msg = None
                        if send_custom:
                            msg = provider.compose({**job, "jd_text": jd})
                            print(f"  [消息] 来源={msg.family} 字数={len(msg.text)}")
                            print(f"  ┌{'─' * 60}")
                            for line in _wrap(msg.text, 58):
                                print(f"  │ {line}")
                            print(f"  └{'─' * 60}")
                            if msg.warnings:
                                print(f"  ⚠ 质检: {'; '.join(msg.warnings)}")
                                if args.cmd == "run":
                                    print("  [✗] 消息有质检告警，已停止；请先检查配置并重新预览。")
                                    stop = True
                                    break

                        attempted += 1

                        if args.preview:
                            print("  [预览] 未发送" if send_custom
                                  else "  [预览] greet-only：只会点「立即沟通」，没有定制消息")
                            if is_new_tab:
                                _maybe_close_tab(session, work_cdp, is_new_tab, base_targets)
                            continue

                        if attempted > 1 and (attempted - 1) % batch_size == 0:
                            print(f"\n  ⏸  已处理 {attempted - 1} 个岗位（已投递 {stats['sent']} 家）。")
                            print("     回车继续下一批 / 输入 q 停止：", end="", flush=True)
                            if _ask("") == "q":
                                stop = True
                                break

                        # ============ 模式 A：只打招呼（当前默认）============
                        # 点「立即沟通」→ BOSS 发出你在「个人中心 → 打招呼语」里设置的那条
                        #   → 弹出「已向BOSS发送消息」→ 点「留在此页」
                        #   → **留在本页**，接着点下一个岗位（不做任何刷新/导航）
                        if not send_custom:
                            # 详情面板是异步渲染的：卡片点开后按钮可能还没画出来。
                            # 等待异步渲染，避免按钮未出现时误判。最多等 3 秒。
                            btn_ready = False
                            for _w in range(8):
                                if element_exists(work_cdp, detail._js_find_visible(
                                        detail.CHAT_SELECTORS)):
                                    btn_ready = True
                                    break
                                time.sleep(0.4)
                            if not btn_ready:
                                stats["failed"] += 1
                                failed_keys.add(key)
                                print("  [✗] 找不到「立即沟通」按钮（等 3 秒仍未出现："
                                      "可能已沟通过 / 页面已改版 / 面板未渲染完）")
                                if is_new_tab:
                                    _maybe_close_tab(session, work_cdp, is_new_tab, base_targets)
                                stop = True
                                break
                            btn_before = detail.chat_button_text()
                            if "继续沟通" in btn_before:
                                # 同 HR 今天已沟通过（按钮是「继续沟通」）—— 点了只是
                                # 打开旧会话，不会再发招呼，也不能记作新联系。
                                stats["dup"] += 1
                                counted.add(key)
                                skip_keys.add(key)
                                print(f"  [已沟通过] 按钮显示「{btn_before}」—— 该 HR 已有会话，"
                                      "不重复打招呼，换下一家")
                                if is_new_tab:
                                    _maybe_close_tab(session, work_cdp, is_new_tab, base_targets)
                                continue

                            # 点「立即沟通」；若弹「今日沟通次数」温馨提示（当天沟通
                            # 满 120 人后每次必弹 + 遮罩吞点击），自动点「好」后重点。
                            r = _click_chat_with_block(detail)
                            if r == "profile_mismatch":
                                _shot(work_cdp, shots_dir, "X_profile_mismatch")
                                if not detail.dismiss_profile_mismatch():
                                    stats["failed"] += 1
                                    failed_keys.add(key)
                                    print("  [✗] BOSS 提示工作经历不匹配且弹窗无法安全关闭，已停止。")
                                    if is_new_tab:
                                        _maybe_close_tab(session, work_cdp, is_new_tab, base_targets)
                                    stop = True
                                    break
                                stats["filtered"] += 1
                                counted.add(key)
                                skip_keys.add(key)
                                print("  [平台拒绝] 工作经历不匹配；未发送招呼语，已跳过该岗位。")
                                if is_new_tab:
                                    _maybe_close_tab(session, work_cdp, is_new_tab, base_targets)
                                greeted = True
                                break
                            if r in ("block", "exhausted"):
                                stats["failed"] += 1
                                failed_keys.add(key)
                                print("  [✗] 平台沟通限制或弹窗未解除，已停止。")
                                if is_new_tab:
                                    _maybe_close_tab(session, work_cdp, is_new_tab, base_targets)
                                stop = True
                                break
                            _shot(work_cdp, shots_dir, "2_clicked_chat")

                            # ---- 招呼是否真发出去了？要证据，不信点击 ----
                            # 证据一：弹出「已向BOSS发送消息」对话框（历史一贯行为，
                            #   点完 1-2 秒内出）；证据二：没弹窗但按钮就地翻转成
                            #   「继续沟通」（BOSS 也可能直接发出不弹窗）。
                            # 两样都没有时结果不确定；不再点击第二次，以免重复发送。
                            ok_greet = (r == "greet") or ("继续沟通" in detail.chat_button_text())
                            if not ok_greet:
                                stats["failed"] += 1
                                contact.mark(key, job, "(发送状态待人工核对)",
                                             extra={"mode": "uncertain"})
                                contacted.add(key)
                                print("  [✗] 无法确认招呼是否发送；已标记待核对并停止。")
                                _shot(work_cdp, shots_dir, "X_greet_uncertain")
                                if is_new_tab:
                                    _maybe_close_tab(session, work_cdp, is_new_tab, base_targets)
                                stop = True
                                break

                            if ok_greet:
                                if detail.greet_dialog_open():
                                    print("     [i] 弹出「已向BOSS发送消息」→ 点「留在此页」")
                                else:
                                    print("     [i] 按钮已翻转成「继续沟通」→ 招呼语已发出（BOSS 未弹窗）")
                                if detail.dismiss_greet_dialog():
                                    print("     [i] 弹窗已关闭，留在本页")
                                else:
                                    print("     [i] 无弹窗需要关闭")
                                _shot(work_cdp, shots_dir, "3_after_dialog")
                            stats["sent"] += 1
                            contact.mark(key, job, "(仅打招呼：使用 BOSS 默认招呼语)",
                                           extra={"mode": "greet_only"})
                            contacted.add(key)
                            _ck = _company_key(job.get("company"))
                            if _ck:
                                company_hits[_ck] = company_hits.get(_ck, 0) + 1
                            print(f"  [✓] 已打招呼（默认招呼语）  已投递 {stats['sent']} 家")
                            if is_new_tab:
                                _maybe_close_tab(session, work_cdp, is_new_tab, base_targets)
                            human_delay(min_d, max_d)
                            greeted = True      # 通知外层：就地重扫，点下一个
                            break

                        # ============ 模式 B：打招呼 + 到聊天页补发定制消息 ============
                        ok_send, note = _send_via_chat(session, work_cdp, job, msg.text, cfg)
                        if is_new_tab:
                            _maybe_close_tab(session, work_cdp, is_new_tab, base_targets)

                        if ok_send:
                            stats["sent"] += 1
                            contact.mark(key, job, msg.text, extra={
                                "family": msg.family, "project": msg.project_id, "hooks": msg.hooks,
                            })
                            contacted.add(key)
                            _ck = _company_key(job.get("company"))
                            if _ck:
                                company_hits[_ck] = company_hits.get(_ck, 0) + 1
                            print(f"  [✓] {note}  累计 {stats['sent']} 条")
                        else:
                            stats["failed"] += 1
                            failed_keys.add(key)
                            print(f"  [✗] 发送失败：{note}")
                            # 点击「立即沟通」可能已发出默认招呼语；把不确定状态记下，
                            # 下一次运行不会自动重复联系。由用户在聊天页核对后处理。
                            contact.mark(key, job, "(发送状态待人工核对)",
                                         extra={"mode": "uncertain"})
                            contacted.add(key)
                            print("      已停止并标记待核对，请在聊天页确认实际发送情况。")
                            stop = True
                            break

                        navigated = True      # 模式 B 会把标签页导航到聊天页 → 重开本页再扫
                        human_delay(min_d, max_d)
                        break

                    if stop or navigated or not greeted:
                        break
                    if rescan > 60:
                        print("  [!] 重扫次数过多，停在本页")
                        break

                if stop:
                    break
                if navigated:
                    # 页面被导航走了（模式 B / 安全校验刷新）：重新打开搜索页接着扫。
                    # 靠 contacted 去重，重扫不会重复投递。
                    if not _search_with_retry(search, query, cfg, city, 1):
                        stop = True
                        break
                    continue              # 重开同一轮，继续扫
                print("  [i] 本轮没有可联系的新岗位了，滚到底加载更多")
                page_no += 1

    if capture_fp is not None:
        capture_fp.close()

    print(f"\n{'=' * 72}")
    if args_scan:
        print(f"[完成] 空跑：命中 {stats['scan_matched']} 个岗位 | "
              f"被过滤 {stats['filtered']} | 已联系过 {stats['dup']}")
        print("       （空跑不点卡片、不开详情、不发消息，只看筛选对不对）")
    elif capture_path:
        print(f"[完成] 采集模式：共写入 {attempted} 个岗位（含 JD）→ {capture_path}")
        print("       下一步：根据该文件生成 messages.json，再用 --messages 发送。")
    elif args.preview:
        print(f"[完成] 只读预览 {attempted} 个岗位 | 已联系过 {stats['dup']} | "
              f"被过滤 {stats['filtered']}；未发送消息")
    else:
        what = "补发定制消息" if send_custom else "打招呼"
        print(f"[完成] {what} {stats['sent']} 家 | 失败 {stats['failed']} | "
              f"已联系过 {stats['dup']} | 被过滤 {stats['filtered']}")
        print(f"       记录文件：{contact.path}")

    # 收尾：别把「已向BOSS发送消息」弹窗留在页面上（下次跑还会碍事）
    try:
        if JobDetailPage(cdp).greet_dialog_open():
            JobDetailPage(cdp).dismiss_greet_dialog()
    except Exception:
        pass
    if guard:
        guard.stop()
        if guard.fixes:
            print(f"[焦点] 运行期间浏览器抢前台 {guard.fixes} 次，"
                  f"已全部还回你原来的窗口（窗口在屏幕外，你看不到它）。")
        else:
            print("[焦点] 运行期间浏览器一次都没抢前台。")
    session.disconnect()
    return 1 if stop or stats["failed"] else 0


# ---------- 辅助 ----------

def _search_with_retry(search: SearchPage, query: str, cfg: dict,
                       city: str = "", page: int = 1) -> bool:
    """打开「城市 + 关键词 + 页码」的搜索页。

    用 URL 导航（`open_search`）而**不是**在页面内打字搜索 —— 页面内搜索换不了城市，
    只会在当前城市的结果里搜。
    实测踩坑：config 写「深圳」、浏览器停在「广州」，页面内搜索拿到的全是广州岗位，
    然后被「城市不符」整批过滤掉，看起来像"搜索没结果"。
    """
    max_retry = cfg["pace"].get("search_max_retry", 3)
    backoff = cfg["pace"].get("search_retry_backoff_s", [15, 25])

    for attempt in range(1, max_retry + 1):
        if not search.on_target_site():
            print(f"  [!] 当前不在 BOSS 页面。请在浏览器里打开 {SearchPage.HOME} 后回车继续...")
            if _ask("") == "q":
                return False
            time.sleep(1.0)
            if not search.on_target_site():
                return False

        if search.open_search(city, query, page):
            print(f"  [*] 搜索成功：{search.title()[:38]} — 本页 {search.card_count()} 个职位")
            return True

        if search.is_refreshed():
            print(f"  [⚠] 搜索「{query}」触发安全校验刷新！手动处理页面后回车继续（q 退出）...")
            if _ask("") == "q":
                return False
            continue

        print(f"  [!] 第 {attempt} 次搜索未拿到结果")
        if attempt < max_retry:
            time.sleep(random.uniform(*backoff))
    return False


def _send_via_chat(session: BrowserSession, cdp, job: dict, text: str,
                   cfg: dict) -> tuple[bool, str]:
    """BOSS 的真实发送链路（2026-10-13 实测）：

        点「立即沟通」 → BOSS 自动发出账号里的默认招呼语
                      → 关掉「已发送」提示框
                      → 导航到聊天页 /web/geek/chat
                      → 点开刚打招呼的那个会话
                      → 输入定制消息并按 Enter

    返回 (是否成功, 说明)。任何一步失败都返回 False，调用方会把该岗位记进
    failed_keys 并在本轮跳过它（而不是静默继续）。
    """
    company = (job.get("company") or "").strip()

    # 1) 点「立即沟通」—— 会顺带把默认招呼语发出去
    detail = JobDetailPage(cdp)
    if detail.chat_button() is None:
        return False, "找不到「立即沟通」按钮（可能已沟通过 / 页面已改版）"
    if not detail.click_chat_button():
        return False, "点「立即沟通」失败（窗口没在前台？）"

    # 2) 关掉「已发送」提示框 —— 关不掉不致命，后面靠导航离开
    detail.dismiss_greet_dialog()

    # 3) 聊天页：点开会话 → 发定制消息
    chat = ChatPage(cdp)
    if not chat.goto():
        return False, "进聊天页失败（/web/geek/chat 没加载出会话列表）"

    matched = chat.open_conversation(match=company) if company else False
    if not matched:
        # 兜底：刚打过招呼的会话一定是最新的（索引 0）。找不到公司名时按最新会话发，
        # 但把这件事说清楚 —— 万一顺序被别人的回复顶乱了，就是发错人。
        if not chat.open_conversation(index=0):
            return False, "聊天页里点不开任何会话"
        if company:
            return False, (f"会话列表里没找到「{company}」，已按最新会话打开但未发送 —— "
                           f"请手动确认后处理（避免发错人）")
        # 公司名为空（岗位卡片没解析出公司），按最新会话发
    return chat.send(text)


def _click_chat_with_block(detail) -> str:
    """点「立即沟通」并等弹窗，返回 'greet' / 'block' / 'nothing' / 'clickfail'。

    2026-10-17 晚故障根因：当天沟通满 120 人后，BOSS 每次点「立即沟通」都会先弹
    「温馨提示：您今天已与120位BOSS沟通，还剩30次沟通机会哦 [好]」，
    同时 .chat-block-layer 遮罩盖住按钮 —— 坐标点击全被遮罩吞掉。
    额度并没有满，点「好」关掉遮罩后再点一次就能正常发。
    150/天真用完时文案变成「您已达到沟通上限…明天再来吧」，按钮是「确定」——
    那种态点了也白点，直接返回 'exhausted' 让上层停批。
    所以这里：点之前先清残留弹窗；等来的若是温馨提示，就点「好」再点一次。
    'block' = 软提示阶段点「好」后重试仍被拦；'exhausted' = 今日额度真用完。
    """
    if detail.chat_block_open():
        detail.dismiss_chat_block()
        time.sleep(0.4)
    if not detail.click_chat_button():
        return "clickfail"
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if detail.greet_dialog_open():
            return "greet"
        if detail.profile_mismatch_open():
            return "profile_mismatch"
        if detail.chat_block_open():
            break
        time.sleep(0.4)
    if not detail.chat_block_open():
        return "nothing"
    # 等来的是「今日沟通次数」弹窗 —— 先分两种态
    if detail.chat_block_exhausted():
        print("     [i] BOSS 提示「今日沟通已达上限」→ 点「确定」关掉弹窗，准备停批")
        detail.dismiss_chat_block()
        return "exhausted"
    # 软提示态：点「好」→ 再点一次「立即沟通」
    print("     [i] 弹「今日沟通次数」温馨提示 → 点「好」后重点")
    detail.dismiss_chat_block()
    time.sleep(0.5)
    if detail.chat_block_open():
        detail.dismiss_chat_block()      # 兜底再点一次
        time.sleep(0.5)
    if detail.chat_block_open():
        return "clickfail"               # 弹窗关不掉
    if not detail.click_chat_button():
        return "clickfail"
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if detail.greet_dialog_open():
            return "greet"
        if detail.profile_mismatch_open():
            return "profile_mismatch"
        if detail.chat_block_open():
            # 重点后又弹了：文案变成"已达上限"就是额度刚好在这家用完
            if detail.chat_block_exhausted():
                detail.dismiss_chat_block()
                return "exhausted"
            return "block"               # 软提示阶段重试仍被拦
        time.sleep(0.4)
    return "nothing"


def _resolve_detail(session: BrowserSession, cdp, base_targets: set):
    """详情可能在同页右侧面板，也可能被弹到新标签页。返回 (要操作的 cdp, 是否新标签页)。"""
    if JobDetailPage(cdp).chat_button() is not None:
        return cdp, False

    for t in session.all_targets():
        if t.get("id") in base_targets:
            continue
        if "zhipin.com" not in (t.get("url") or ""):
            continue
        try:
            new = session.cdp.__class__(session.cdp_url)
            new.attach(t)
            if JobDetailPage(new).chat_button() is not None:
                print("  [i] 详情在新标签页里，已切换过去")
                return new, True
            new.close()
        except Exception:
            continue
    return cdp, False


def _maybe_close_tab(session: BrowserSession, cdp, is_new_tab: bool, base_targets: set):
    """结束一个岗位后：若之前切到了新标签页，回到主页面。"""
    if not is_new_tab:
        return
    try:
        cdp.close()
    except Exception:
        pass


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


# ---------- 子命令 ----------

def _add_common(p: argparse.ArgumentParser):
    """所有连浏览器的子命令都有的参数。"""
    p.add_argument("--config", default="", help="配置文件路径（默认读当前目录 config.yaml）")
    p.add_argument("--profile", default="", help="个人档案路径（默认读当前目录 profile.yaml）")
    p.add_argument("--city", default="",
                   help="覆盖配置里的城市，支持单城/逗号分隔/城市组（如 珠三角）")
    p.add_argument("--query", default="", help="只跑指定的一个搜索词")
    p.add_argument("--limit", type=int, default=0, help="本次最多处理多少条（0=按配置）")
    p.add_argument("--pages", type=int, default=0,
                   help="覆盖配置里的 max_pages（滚到底加载的轮数）")
    p.add_argument("--cdp-input", action="store_true",
                   help="用 CDP 派发事件（不动真实鼠标、不抢前台；窗口不能最小化）")
    p.add_argument("--os-input", action="store_true",
                   help="用 pyautogui 真实键鼠（会占用鼠标、需前台）")
    p.add_argument("--show-window", action="store_true",
                   help="不把浏览器窗口挪到屏幕外（默认会挪走：不占屏幕、不抢鼠标）")
    p.add_argument("--shots", default="",
                   help="把每一步关键动作截图存到该目录（用于确认真的执行到位）")
    return p


def _add_outreach(p: argparse.ArgumentParser):
    """只有真会投递的子命令才有的参数。"""
    p.add_argument("--greet-only", action="store_true",
                   help="只点「立即沟通」（用 BOSS 默认招呼语），不发定制消息")
    p.add_argument("--custom", action="store_true",
                   help="打招呼后，再到聊天页补发一条按 JD 定制的消息")
    p.add_argument("--messages", default="", help="external 模式用的消息文件（默认取配置）")
    return p


def cmd_export(args) -> int:
    """导出投递清单（不需要连浏览器）。"""
    src = args.source or "contacted.json"
    p = Path(src)
    if not p.is_absolute():
        p = Path.cwd() / p
    history = load_history(p)
    if not history:
        print(f"[i] {p} 里没有记录，没什么可导出的。")
        return 0
    written = export_contacted(history, args.out_md, args.out_html, title=args.title)
    print(f"[✓] 已导出 {len(history)} 条 → {', '.join(str(w) for w in written)}")
    return 0


def cmd_init(args) -> int:
    """把空白配置放到用户选择的本地目录；绝不覆盖已有文件。"""
    dest = Path(args.dir).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    source = resources.files("bossgreet").joinpath("examples")
    for name in ("config.yaml", "profile.yaml"):
        target = dest / name
        if target.exists():
            print(f"[i] 已存在，未覆盖：{target}")
            continue
        example = source.joinpath(name.replace(".yaml", ".example.yaml"))
        target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[✓] 已创建：{target}")
    print("请编辑这两个文件，尤其是搜索词、城市和个人档案；然后运行 preview。")
    return 0


def cmd_info(args) -> int:
    """打印当前配置摘要（不连浏览器，用来确认配置读对了没）。"""
    cfg = load_config(args.config)
    pf = load_profile(args.profile)
    cities_allow, search_cities = _resolve_cities(args.city or "", cfg)
    pace = cfg.get("pace", {}) or {}
    msg = cfg.get("message", {}) or {}
    f = cfg.get("filter", {}) or {}
    print("=" * 60)
    print("配置摘要")
    print("=" * 60)
    browser = cfg.get("browser", {})
    print(f"  浏览器     : {browser.get('name', 'edge')} / {browser.get('cdp_url', '-')}"
          f"（自动启动 {browser.get('auto_start', True)}；输入通道 {browser.get('input_mode', 'cdp')}）")
    print(f"  搜索词     : {len(cfg.get('search_queries') or [])} 个"
          f"　{', '.join((cfg.get('search_queries') or [])[:5])}{'…' if len(cfg.get('search_queries') or []) > 5 else ''}")
    print(f"  可投城市   : {'、'.join(cities_allow) or '不限'}")
    print(f"  本批搜索   : {'、'.join(search_cities) or '不限'}")
    print(f"  节奏       : 单次上限 {pace.get('daily_limit', '-')}、"
          f"批次 {pace.get('batch_size', '-')}、"
          f"间隔 {pace.get('min_delay_s', '-')}~{pace.get('max_delay_s', '-')} 秒")
    print(f"  消息生成器 : {msg.get('generator', 'template')}"
          f"（上限 {msg.get('max_chars', '-')} 字）")
    print(f"  单公司上限 : {f.get('max_per_company', 0) or '不限'}")
    print(f"  黑名单公司 : {len(f.get('skip_companies') or [])} 个")
    print(f"  关键词     : 目标 {len(cfg.get('keywords') or [])}、"
          f"必含 {len(cfg.get('must_keywords') or [])}、"
          f"性质 {len(cfg.get('entry_keywords') or [])}、"
          f"排除 {len(cfg.get('exclude_keywords') or [])}、"
          f"排除标签 {len(cfg.get('exclude_tags') or [])}")
    basic = pf.get("basic", {}) or {}
    if basic.get("name"):
        print(f"  投递身份   : {basic.get('name')}（{basic.get('education', '-')}）")
    else:
        print("  投递身份   : 未填姓名（消息里用「我」）")
    cpath = Path(os.environ.get("BOSSGREET_CONTACTED")
                 or state_path("contacted", "contacted.json", cfg))
    print(f"  记录文件   : {cpath}"
          f"（已记录 {len(load_history(cpath))} 条）")
    print("=" * 60)
    return 0


def build_parser() -> argparse.ArgumentParser:
    from bossgreet import __version__
    ap = argparse.ArgumentParser(
        prog="bossgreet",
        description="BOSS 直聘岗位扫描与打招呼自动化（裸 CDP，不使用 Playwright）")
    ap.add_argument("--version", action="version", version=f"bossgreet {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="<命令>")

    p_run = _add_common(sub.add_parser(
        "run", help="正式跑：筛岗位 → 打招呼（真的会动你的账号）"))
    _add_outreach(p_run)
    p_run.add_argument("--send", action="store_true",
                       help="明确允许真实发送；执行前仍需人工输入 send 确认")

    p_prev = _add_common(sub.add_parser(
        "preview", help="预览：完整走筛选并读 JD、生成消息，但一条都不发"))
    _add_outreach(p_prev)

    p_scan = _add_common(sub.add_parser(
        "scan", help="空跑：只扫列表按规则过滤，不点卡片、不开详情、不发消息"))
    p_scan.add_argument("--grep", default="",
                        help="只打印标题含该词的岗位（用来查某个岗有没有被判对）")

    p_cap = _add_common(sub.add_parser(
        "capture", help="采集：把命中的岗位 + JD 存成 jsonl，不生成也不发送"))
    p_cap.add_argument("--out", default="", help="输出文件（默认取配置 paths.jobs）")

    p_exp = sub.add_parser("export", help="导出投递清单（md/html），不需要连浏览器")
    p_exp.add_argument("--source", default="", help="记录文件（默认 contacted.json）")
    p_exp.add_argument("--out-md", default="投递清单.md", help="Markdown 输出路径")
    p_exp.add_argument("--out-html", default="", help="HTML 输出路径（不给就不生成）")
    p_exp.add_argument("--title", default="投递清单", help="清单标题")

    p_info = sub.add_parser("info", help="打印当前配置摘要，不连浏览器")
    p_info.add_argument("--config", default="", help="配置文件路径")
    p_info.add_argument("--profile", default="", help="个人档案路径")
    p_info.add_argument("--city", default="", help="城市/城市组，用于预览解析结果")

    p_init = sub.add_parser("init", help="在本地目录创建空白配置和档案，不覆盖已有文件")
    p_init.add_argument("--dir", default=".", help="本地工作目录（默认当前目录）")

    # 让 _pipeline 无论走到哪个子命令都能拿到全部属性，不必到处 getattr
    defaults = dict(preview=False, scan=False, capture="", messages="",
                    greet_only=False, custom=False, pages=0, grep="")
    for p in (p_run, p_prev, p_scan, p_cap, p_exp, p_info, p_init):
        p.set_defaults(**defaults)
    return ap


def main(argv=None) -> int:
    # Windows 终端可能使用 GBK，少数状态符号不可编码；保持中文输出可用。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = build_parser()
    args = ap.parse_args(["preview"] if argv == [] else argv)
    if not getattr(args, "cmd", None):
        args = ap.parse_args(["preview"])

    if args.cmd == "export":
        return cmd_export(args)
    if args.cmd == "init":
        return cmd_init(args)
    if args.cmd == "info":
        return cmd_info(args)

    if args.cmd == "run":
        if not args.send:
            ap.error("真实发送需要显式指定 run --send；只读操作请使用 preview")
        print("真实发送会先触发 BOSS 账号的默认招呼语。输入 send 开始第一批；其他输入停止：",
              end="", flush=True)
        if _ask("") != "send":
            print("已取消，未连接浏览器。")
            return 1

    # 以下子命令共用同一条流水线，只是"要不要真的动手"不同
    if args.cmd == "preview":
        args.preview = True
    if args.cmd == "scan":
        args.scan = True
    if args.cmd == "capture":
        args.capture = args.out
    return _pipeline(args)


if __name__ == "__main__":
    sys.exit(main())
