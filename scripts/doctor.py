# -*- coding: utf-8 -*-
"""环境体检：把「跑不通」的确切原因一次性定位出来。

检查项：
  1. 调试端口 / 浏览器
  2. 浏览器错位（脚本连的浏览器 ≠ 你会去点的那个浏览器）
  3. 登录态（薪资栏是否为空 / 是否有登录提示）
  4. 当前页是否 BOSS 职位页、是否被安全校验拦截
  5. 选择器实测（卡片 / JD / 沟通按钮 / 聊天输入框）
  6. 反调试存活观察（附着后页面是否被 BOSS 干掉）
  7. DOM 结构采样（选择器失配时按这个改 pages/zhipin.py）

用法：
  python scripts/doctor.py                 # 打印报告
  python scripts/doctor.py --report r.txt  # 同时写到文件（方便贴给我看）
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bossgreet.core.cdp import Cdp, CdpError                              # noqa: E402
from bossgreet.core.config import load_config                             # noqa: E402
from bossgreet.core.human import enum_browser_windows, browser_exe_from_ua  # noqa: E402
from bossgreet.pages.zhipin import SearchPage, JobDetailPage               # noqa: E402

OK = "  ✓"
NG = "  ✗"
WARN = "  ! "

_BUF: list[str] = []


def say(*a):
    line = " ".join(str(x) for x in a)
    print(line)
    _BUF.append(line)


def head(t):
    say("")
    say("=" * 72)
    say("  " + t)
    say("=" * 72)


def main():
    report = ""
    for i, a in enumerate(sys.argv):
        if a == "--report" and i + 1 < len(sys.argv):
            report = sys.argv[i + 1]

    cfg = load_config()
    base = cfg["browser"]["cdp_url"]
    say(f"体检目标：{base}")

    # ---------- 1. 端口 / 浏览器 ----------
    head("1. 调试端口 / 浏览器")
    c = Cdp(base)
    try:
        v = c.version()
        say(f"{OK} 端口可用：{v.get('Browser')}")
        ua = v.get("User-Agent", "")
    except Exception as e:
        say(f"{NG} 连不上 {base}：{e}")
        say('    运行 bossgreet preview 会按 config.yaml 的 browser.name 自动启动浏览器')
        _flush(report)
        return

    exe = browser_exe_from_ua(ua)
    say(f"{OK} 该浏览器进程名应为：{exe}")

    try:
        targets = c.list_targets()
    except Exception as e:
        say(f"{NG} 读取 target 失败：{e}")
        _flush(report)
        return
    say(f"    当前有 {len(targets)} 个页面 target")

    # ---------- 2. 浏览器错位 ----------
    head("2. 浏览器错位检查")
    say("  逻辑：系统里「标题含 BOSS」的窗口，是否就是脚本连的这个浏览器。")
    say("       不是的话，脚本会在 A 浏览器里读、在 B 浏览器里点，必然失败。")
    wins = enum_browser_windows()

    def is_boss(w):
        t = w["title"].upper()
        return "直聘" in w["title"] or "BOSS" in t

    for w in wins:
        tag = ""
        if w["exe"] == exe and is_boss(w):
            tag = "  ← 脚本连的这个浏览器里的 BOSS 窗口"
        say(f"    [{w['exe']:11s}] {w['title'][:60]}{tag}")

    boss_targets = [t for t in targets if "zhipin.com" in (t.get("url") or "")]
    same_exe_boss = [w for w in wins if w["exe"] == exe and is_boss(w)]
    other_boss = [w for w in wins if w["exe"] != exe and is_boss(w)]

    if boss_targets:
        say(f"{OK} 脚本所连浏览器里有 {len(boss_targets)} 个 BOSS 页面 → 一致，正常")
    elif same_exe_boss:
        say(f"{WARN}该浏览器窗口标题是 BOSS，但调试端口里没有 BOSS 页面 target")
    elif other_boss:
        say(f"{NG} 脚本所连浏览器里【没有】BOSS 页面，但另一个浏览器有 BOSS 窗口：")
        for w in other_boss:
            say(f"       → [{w['exe']}] {w['title'][:60]}")
        say("    处理：光在另一个浏览器打开 BOSS 没用 —— 必须让【带调试端口的这个浏览器】有 BOSS 页。")
        say("          运行： python scripts/open_boss.py --open")
    else:
        say(f"{NG} 系统里没有任何 BOSS 窗口")
        say("    运行： python scripts/open_boss.py --open")

    # ---------- 附着 ----------
    t = c.find_target("zhipin.com")
    if t is None:
        say("")
        say("[!] 没有 BOSS 页面可附着，后续检查无法进行。先跑 scripts/open_boss.py --open")
        _flush(report)
        return
    say("")
    say(f"[*] 附着到：{(t.get('title') or '')[:50]}")
    c.attach(t)

    # ---------- 3. 登录态 ----------
    head("3. 登录态")
    _login_check(c)

    # ---------- 4. 页面就位 ----------
    head("4. 当前页是否 BOSS 职位页")
    url = c.url()
    say(f"    URL: {url[:95]}")
    say(f"    on_target_site = {'zhipin.com' in url}")
    say(f"    is_refreshed   = {'_security_check' in url}  (True 表示被安全校验拦截)")

    # ---------- 5. 选择器实测 ----------
    head("5. 选择器实测（命中数量）")
    # 选择器直接取自 pages/zhipin.py，避免两边漂移（这正是之前
    # 「沟通按钮 0 命中」被漏掉的根因：doctor 里写的是另一套旧选择器）。
    groups = {
        "职位卡片": [s.strip() for s in SearchPage.CARD_SEL.split(",")],
        "JD 正文容器": JobDetailPage.JD_SELECTORS,
        "沟通按钮": JobDetailPage.CHAT_SELECTORS,
        "聊天输入框": JobDetailPage.INPUT_SELECTORS,
    }
    for gname, sels in groups.items():
        say("")
        say(f"  【{gname}】")
        for s in sels:
            say(f"    {_count(c, s):5d}  {s}")
    say("")
    say(f"    文本精确匹配「立即沟通」的元素数：{_count_by_text(c, '立即沟通')}")

    # ---------- 6. 反调试存活观察 ----------
    head("6. 反调试存活观察（附着后页面是否被 BOSS 干掉）")
    alive = 0
    for i in range(6):
        time.sleep(1)
        try:
            c.evaluate("1+1")
            alive += 1
            say(f"    +{i + 1}s  存活")
        except CdpError as e:
            say(f"    +{i + 1}s  失败：{str(e)[:70]}")
            break
    if alive >= 6:
        say(f"{OK} 6 秒内页面稳定（裸 CDP 未被 BOSS 反调试命中）")
    else:
        say(f"{NG} 页面在 {alive} 秒内失去响应 —— 检查代码里是否混用了 Playwright / 启用了调试域")

    # ---------- 7. DOM 采样 ----------
    head("7. DOM 结构采样（选择器失配时按这个改 pages/zhipin.py）")
    try:
        sample = c.evaluate_json(r"""
const cards = document.querySelectorAll('li.job-card-wrapper, li.job-card-box, .job-card-box');
const out = ['卡片数量: ' + cards.length];
const first = cards[0];
if (first) {
  out.push('第一个卡片骨架:');
  const walk = (el, d) => {
    if (d > 3) return;
    for (const ch of el.children) {
      const cls = (typeof ch.className === 'string') ? ch.className : '';
      const txt = (ch.innerText || '').trim().replace(/\n/g, ' ').slice(0, 30);
      if (cls) out.push('  '.repeat(d) + ch.tagName.toLowerCase() + '.' +
                        cls.split(' ').slice(0, 2).join('.') +
                        (txt ? ' | ' + JSON.stringify(txt) : ''));
      walk(ch, d + 1);
    }
  };
  walk(first, 0);
}
out.push('沟通按钮存在: ' + (!!document.querySelector("%(CHAT)s") ||
  Array.from(document.querySelectorAll('a,button')).some(e => (e.innerText||'').trim() === '立即沟通')));
out.push('聊天输入框存在: ' + !!document.querySelector("%(INPUT)s"));
return out;
""" % {"CHAT": ", ".join(JobDetailPage.CHAT_SELECTORS),
       "INPUT": ", ".join(JobDetailPage.INPUT_SELECTORS)})
        for line in sample or []:
            say("   ", line)
    except Exception as e:
        say("    采样失败:", e)

    head("结论")
    say("  · 第 2 步报「另一个浏览器有 BOSS 窗口」→ 跑 scripts/open_boss.py --open")
    say("  · 第 3 步报未登录 → 在【带调试端口的那个浏览器】里登录一次 BOSS（扫码即可）")
    say("  · 第 5 步某组命中全为 0 → BOSS 改版，按第 7 步采样改 pages/zhipin.py 的选择器")
    say("  · 第 6 步不通过 → 代码里混进了 Playwright，换回 core.cdp")
    c.close()
    _flush(report)


def _flush(report: str):
    if report:
        Path(report).write_text("\n".join(_BUF), encoding="utf-8")
        print(f"\n[报告已写入 {report}]")


def _count(c: Cdp, sel: str) -> int:
    try:
        return int(c.evaluate(f"document.querySelectorAll({sel!r}).length") or 0)
    except Exception:
        return 0


def _count_by_text(c: Cdp, text: str) -> int:
    try:
        return int(c.evaluate_json(
            "const t = %r;"
            "return Array.from(document.querySelectorAll('a,button,span,div'))"
            ".filter(e => (e.innerText || '').trim() === t).length;" % text) or 0)
    except Exception:
        return 0


def _login_check(c: Cdp):
    """三条判据互相印证：薪资栏是否全空 / 页面是否出现登录提示 / 有没有 geek_zp_token。"""
    try:
        sal = c.evaluate_json(r"""
const sal = document.querySelectorAll('.job-salary');
if (!sal.length) return null;
let empty = 0;
sal.forEach(n => { if (!(n.innerText || '').trim()) empty++; });
return {total: sal.length, empty: empty};
""")
    except Exception:
        sal = None
    if sal:
        ratio = sal["empty"] / sal["total"]
        if ratio >= 0.8:
            say(f"{NG} 薪资栏绝大多数为空（空 {sal['empty']}/{sal['total']}）→ 提示【未登录】")
            say("      （不用「全空」是因为列表末尾可能混有 1 条带薪资的推荐位）")
        else:
            say(f"{OK} 薪资栏有内容（空 {sal['empty']}/{sal['total']}）→ 看起来【已登录】")
    else:
        say("    （当前页面没有薪资栏，无法据此判断）")

    try:
        wall = c.evaluate_json(r"""
const t = document.body ? (document.body.innerText || '') : '';
return ['立即登录', '登录账号', '扫码登录', '手机号登录', '请先登录'].filter(k => t.includes(k));
""") or []
        if wall:
            say(f"{NG} 页面出现登录提示：{wall}")
        else:
            say(f"{OK} 页面文本里没有登录提示")
    except Exception as e:
        say(f"    登录字样检测失败：{e}")

    # 判据三（最可靠）：顶栏用户入口。登录后是「消息 / 简历 / 你的名字」，
    # 未登录时同一位置是「登录/注册」。
    try:
        nav = c.evaluate_json(r"""
const nav = document.querySelector('.user-nav');
const t = nav ? (nav.innerText || '').replace(/\s+/g, ' ').trim() : '';
return {
  text: t.slice(0, 40),
  has_avatar: !!(nav && nav.querySelector('img, .nav-figure, [class*="avatar"]')),
  has_resume: t.includes('简历'),
  has_login: /登录|注册/.test(t),
};
""") or {}
        if nav.get("has_resume") or nav.get("has_avatar"):
            say(f"{OK} 顶栏出现用户入口 → 已登录")
        elif nav.get("has_login"):
            say(f"{NG} 顶栏显示登录入口 → 未登录")
        else:
            say("    顶栏状态无法判断")
    except Exception as e:
        say(f"    顶栏检测失败：{str(e)[:70]}")

    # 诊断报告不会读取或打印 Cookie 与账号文字。


if __name__ == "__main__":
    main()
