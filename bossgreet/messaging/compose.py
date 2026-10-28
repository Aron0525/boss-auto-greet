# ============ 消息生成：岗位内容 → 定制招呼语 ============
# 纯本地规则，不调任何外部 API：给定岗位（标题 + JD 正文）与我的 profile，
# 组装出一条"看得出是冲着这个岗位写的"招呼语。
#
# 设计要点：
#   1. ASCII 短标签（C / AD / PCB）必须做词边界匹配，否则 "C" 会命中 "PCB"、"CAN"。
#   2. 项目按 tags 命中数排序挑最相关的那个 —— 这是"定制感"的主要来源。
#   3. 开场/结尾从同义句池随机取，避免所有消息长得一模一样（反群发感）。
#   4. 生成完必须过质检：长度、至少含公司名或职位名、无残留占位符。

from __future__ import annotations

import argparse
import random
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 允许 `python message/compose.py` 与 `python -m message.compose` 两种跑法
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bossgreet.core.config import load_profile, load_templates  # noqa: E402

_ASCII_RE = re.compile(r"^[\x00-\x7f]+$")


def _tag_hit(tag: str, hay: str) -> bool:
    """tag 是否命中 haystack。

    中文标签：子串匹配即可。
    ASCII 标签：必须落在词边界上——否则标签 "C" 会命中 "PCB"、"CAN"、"MCU"，
    标签 "AD" 会命中 "LOAD"。这是最容易踩的坑。

    ⚠ 两侧都要小写：以前只把 tag 转小写、却拿去匹配没转小写的原文，
      于是 "FreeRTOS" 这类标签对 JD 里的「熟悉 FreeRTOS」永远命中不上 ——
      只有在调用方碰巧先把整段文本 lower 过时才"看起来正常"。
      与其依赖调用方的隐式约定，不如在这里做掉。
    """
    if not tag:
        return False
    t = tag.lower()
    hay = (hay or "").lower()
    if _ASCII_RE.match(tag):
        return re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])",
                         hay) is not None
    return t in hay


def _hits(tags, hay: str) -> list[str]:
    return [t for t in (tags or []) if _tag_hit(str(t), hay)]


@dataclass
class Message:
    text: str
    family: str = ""
    project_id: str = ""
    hooks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return self.text


class Composer:
    """把岗位内容组装成定制消息。一次运行内记住已生成的消息，用于去重。"""

    def __init__(self, profile: dict, templates: dict, seed: int | None = None):
        self.profile = profile or {}
        self.tpl = templates or {}
        self.settings = self.tpl.get("settings", {}) or {}
        self.rng = random.Random(seed)
        self._seen: set[str] = set()

    # ---------- 各步骤 ----------

    def pick_family(self, title: str) -> dict:
        t = (title or "").lower()
        for fam in self.tpl.get("families", []) or []:
            kws = fam.get("match") or []
            if not kws:          # match 为空 = 兜底族
                return fam
            if any(str(k).lower() in t for k in kws):
                return fam
        return {"id": "general", "positioning_key": "general"}

    def pick_project(self, hay: str) -> dict | None:
        """按 tags 命中数挑最相关的项目；命中为 0 时退化为第一个项目。"""
        projects = self.profile.get("projects") or []
        if not projects:
            return None
        scored = [(len(_hits(p.get("tags"), hay)), i, p) for i, p in enumerate(projects)]
        scored.sort(key=lambda x: (-x[0], x[1]))
        return scored[0][2]

    def pick_hooks(self, hay: str) -> list[str]:
        limit = int(self.settings.get("max_tech_hooks", 2))
        out: list[str] = []
        for hook in self.tpl.get("tech_hooks", []) or []:
            if len(out) >= limit:
                break
            if _hits(hook.get("tags"), hay):
                out.append(str(hook.get("text", "")).strip())
        return [h for h in out if h]

    def pick_skills(self, hay: str) -> list[str]:
        limit = int(self.settings.get("max_skills", 3))
        skills = self.profile.get("skills") or []
        scored = [(len(_hits(s.get("tags"), hay)), i, s) for i, s in enumerate(skills)]
        scored.sort(key=lambda x: (-x[0], x[1]))
        names = [str(s.get("name")) for score, _, s in scored if score > 0]
        if not names:   # 一个都没命中时，取前 limit 个通用技能
            names = [str(s.get("name")) for s in skills[:limit]]
        return names[:limit]

    # ---------- 组装 ----------

    def compose(self, job: dict, rerolls: int = 5) -> Message:
        title = (job.get("title") or "").strip()
        company = (job.get("company") or "").strip()
        jd = (job.get("jd_text") or "").strip()
        city = (job.get("city") or "").strip()
        hay = f"{title} {jd}".lower()

        fam = self.pick_family(title)
        pos_key = fam.get("positioning_key") or "general"
        positioning = (self.profile.get("positioning") or {}).get(pos_key) or \
            (self.profile.get("positioning") or {}).get("general") or "求职者"

        openings = (self.tpl.get("openings") or {}).get(fam.get("id")) or \
            (self.tpl.get("openings") or {}).get("general") or []
        # 知道公司名时，优先用点名公司的那几种开场（比"贵司"更有诚意，也更不像群发）
        if company and openings:
            named = [o for o in openings if "{company" in str(o)]
            if named:
                openings = named
        closings = self.tpl.get("closings") or []

        project = self.pick_project(hay)
        hooks = self.pick_hooks(hay)
        skills = self.pick_skills(hay)
        allow_traits = bool(self.settings.get("allow_traits", True))
        trait = ""
        if not hooks and allow_traits:
            pool = self.profile.get("traits") or []
            if pool:
                trait = self.rng.choice(pool)

        slots = {
            "title": title or "该岗位",
            "company": company,
            "company_or_guisi": company or "贵司",
            "city": city,
            "positioning": positioning,
            "name": self.profile.get("basic", {}).get("name", ""),
            "education": self.profile.get("basic", {}).get("education", ""),
            "school": self.profile.get("basic", {}).get("school", ""),
            "availability": self.profile.get("basic", {}).get("availability", ""),
        }

        best_text, best_warn = "", ["空消息"]
        for _ in range(max(1, rerolls)):
            parts = self._build_parts(slots, openings, closings, project, hooks, skills, trait)
            text, warns = self._assemble(parts)
            if not warns:
                best_text, best_warn = text, []
                break
            if not best_text:
                best_text, best_warn = text, warns

        # 必含校验：至少出现公司名或职位名，否则像群发
        need_any = self.settings.get("must_contain_any") or []
        if "company" in need_any and company and company not in best_text:
            if "title" not in need_any or (title and title not in best_text):
                best_warn.append("既无公司名也无职位名")

        self._seen.add(best_text)
        return Message(
            text=best_text,
            family=fam.get("id", ""),
            project_id=(project or {}).get("id", ""),
            hooks=hooks,
            warnings=best_warn,
        )

    def _build_parts(self, slots, openings, closings, project, hooks, skills, trait) -> list[tuple[str, str]]:
        """返回 [(段落key, 文本)]，key 用于按优先级裁剪。"""
        tpl = self.tpl
        parts: list[tuple[str, str]] = []

        if openings:
            parts.append(("opening", self.rng.choice(openings).format(**slots)))

        if project:
            oneliner = str(project.get("oneliner", "")).strip()
            if oneliner:
                parts.append(("project", str(tpl.get("project", {}).get("oneliner", "{project_oneliner}"))
                              .format(**slots, project_oneliner=oneliner)))
            hi_pool = project.get("highlights") or []
            if hi_pool:
                hi = self.rng.choice(hi_pool)
                parts.append(("highlight", str(tpl.get("project", {}).get("highlight", "{project_highlight}"))
                              .format(**slots, project_highlight=hi)))

        if hooks:
            parts.append(("hooks", "另外" + "；".join(hooks)))

        if skills:
            sk = "、".join(skills)
            # 中英混排：技能名以 ASCII 开头时补一个空格，避免「用C 语言」这种挤在一起
            if sk and ord(sk[0]) < 128:
                sk = " " + sk
            parts.append(("skills", str(tpl.get("skills", {}).get("lead_in", "平时主要用{skills}"))
                          .format(**slots, skills=sk)))

        if trait:
            parts.append(("traits", str(tpl.get("traits", {}).get("lead_in", "{trait}"))
                          .format(**slots, trait=trait)))

        if closings:
            parts.append(("closing", self.rng.choice(closings)))

        return parts

    def _assemble(self, parts: list[tuple[str, str]]) -> tuple[str, list[str]]:
        sep = self.tpl.get("assembly", {}).get("separator", "。")
        order = self.tpl.get("assembly", {}).get("order")
        if order:
            rank = {k: i for i, k in enumerate(order)}
            parts = sorted(parts, key=lambda p: rank.get(p[0], 99))

        # 裁剪优先级：从尾部开始丢这些可选段落
        droppable = ["traits", "skills", "hooks", "highlight"]
        max_chars = int(self.settings.get("max_chars", 300))

        def render(ps) -> str:
            return sep.join(t for _, t in ps if t).strip()

        text = render(parts)
        for key in droppable:
            if len(text) <= max_chars:
                break
            parts = [p for p in parts if p[0] != key]
            text = render(parts)

        warns: list[str] = []
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
            warns.append(f"超长已截断（>{max_chars}）")
        if len(text) < int(self.settings.get("min_chars", 70)):
            warns.append(f"偏短（{len(text)} 字）")
        if re.search(r"\{[a-z_]+\}", text):
            warns.append("存在未替换的占位符")
        if text in self._seen:
            warns.append("与上一条重复")
        return text, warns


# ============ 共用的质检：模板生成和 LLM 生成都要过这一关 ============

def validate_text(text: str, job: dict, settings: dict) -> list[str]:
    """对一条待发消息做质检，返回问题列表（空列表 = 通过）。"""
    warns: list[str] = []
    text = text or ""
    max_chars = int(settings.get("max_chars", 300))
    min_chars = int(settings.get("min_chars", 70))

    if len(text) > max_chars:
        warns.append(f"超长（{len(text)} > {max_chars}）")
    if len(text) < min_chars:
        warns.append(f"偏短（{len(text)} 字）")
    if re.search(r"[{}]", text):
        warns.append("含花括号（疑似残留占位符）")

    company = (job.get("company") or "").strip()
    title = (job.get("title") or "").strip()
    has_company = bool(company) and company in text
    has_title = bool(title) and title in text
    # 职位名常被写成简称，做一次去括号/去后缀的宽松匹配
    if title and not has_title:
        short = re.split(r"[（(（]", title)[0].strip()
        if short and short in text:
            has_title = True
    if not has_company and not has_title:
        warns.append("既无公司名也无职位名（像群发）")
    return warns


# ============ 命令行：离线试生成 ============

def main():
    ap = argparse.ArgumentParser(description="离发生成定制招呼语（不发送）")
    ap.add_argument("--title", required=True, help="职位名，如 '嵌入式软件工程师'")
    ap.add_argument("--company", default="", help="公司名")
    ap.add_argument("--city", default="", help="城市")
    ap.add_argument("--jd", default="", help="JD 正文（字符串）")
    ap.add_argument("--jd-file", default="", help="JD 正文所在文件（UTF-8）")
    ap.add_argument("--n", type=int, default=1, help="生成几条（观察变体）")
    ap.add_argument("--seed", type=int, default=None, help="固定随机种子，便于复现")
    args = ap.parse_args()

    jd = args.jd
    if args.jd_file:
        jd = Path(args.jd_file).read_text(encoding="utf-8")

    profile = load_profile()
    templates = load_templates()
    comp = Composer(profile, templates, seed=args.seed)

    job = {"title": args.title, "company": args.company, "city": args.city, "jd_text": jd}
    for i in range(args.n):
        msg = comp.compose(job)
        print(f"--- [{i + 1}] 岗位族={msg.family} 项目={msg.project_id} "
              f"钩子={len(msg.hooks)} 字数={len(msg.text)} ---")
        print(msg.text)
        if msg.warnings:
            print(f"  ⚠ 质检: {'; '.join(msg.warnings)}")
        print()


if __name__ == "__main__":
    main()
