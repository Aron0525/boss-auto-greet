# ============ 命令层：岗位过滤 ============
from __future__ import annotations

import re
from typing import Any

from bossgreet.core.salary import decode_salary, has_unknown_pua


def _hit(kw: str, text: str) -> bool:
    """关键词命中判断。

    中文/符号词：子串匹配（原行为）。
    ASCII 词：**词边界 + 不区分大小写**。

    为什么必须这么改（2026-10-17 空跑暴露）：
      BOSS 的标题大小写很乱 —— 「Ai agent（应届生可投）」「ai应用开发」都存在，
      而原来的 `kw in title` 是严格区分大小写的，
      「AI」「Agent」这类词会把小写写法整批误杀（实例：标题写 `Ai agent` → 判「无匹配关键词」）。
      同时不能简单 `kw.lower() in title.lower()`：那样 "AI" 会命中 "Email"、"Detail"，
      "RAG" 会命中 "storage"。所以对 ASCII 词加左右字母边界。
      （"AI应用开发" 里 AI 右边是中文，不算字母，仍能命中。）
    """
    if not kw:
        return False
    if not kw.isascii():
        return kw in text
    pat = r"(?<![A-Za-z])" + re.escape(kw) + r"(?![A-Za-z])"
    return re.search(pat, text, re.IGNORECASE) is not None


def _target_cities(cfg: dict) -> list[str]:
    """取「可投城市集合」。优先 filter.cities（列表），向后兼容 filter.city（单个）。

    为什么要有集合：BOSS 的搜索结果不按行政边界切 —— 实测搜「广州」会混进
    `佛山·顺德区·北滘` 的岗位（广佛同城）。若过滤只认单个城市，
    这些本该投的珠三角岗位会被「城市不符」整批误杀（2026-10-17 踩过，广州只投出 1 家）。
    """
    f = cfg.get("filter", {}) or {}
    allow = f.get("cities") or []
    if isinstance(allow, str):
        allow = [x.strip() for x in allow.replace("，", ",").replace("、", ",").split(",")
                 if x.strip()]
    if not allow:
        single = (f.get("city") or "").strip()
        allow = [single] if single else []
    return allow


def matches(job: dict[str, Any], cfg: dict) -> tuple[bool, str]:
    """判断一个岗位是否值得联系。返回 (是否通过, 原因)。

    job 字段：title / salary / city / company。
    """
    title = str(job.get("title", ""))
    salary = str(job.get("salary", ""))
    city = str(job.get("city", ""))

    # 1. 城市（集合匹配：命中任一即算符合）
    target_cities = _target_cities(cfg)
    if target_cities and city:
        if not any(t in city for t in target_cities):
            return False, f"城市不符（限{'/'.join(target_cities)}）：{city}"

    # 2. 排除词（优先级最高）
    for kw in cfg.get("exclude_keywords", []) or []:
        if _hit(kw, title):
            return False, f"命中排除词：{kw}"

    # 2.5 排除标签（学历门槛明显低于本科的岗，如「中专/中技」）
    tags = [str(t) for t in (job.get("tags") or [])]
    tag_blob = " ".join(tags)
    for kw in cfg.get("exclude_tags", []) or []:
        if _hit(kw, tag_blob):
            return False, f"命中排除标签：{kw}"

    # 3. 目标关键词
    hit = [kw for kw in (cfg.get("keywords") or []) if _hit(kw, title)]
    if not hit:
        return False, "无匹配关键词"

    # 3.5 方向必含词 —— 职位名必须命中至少一个，用来锁死职能方向。
    #     场景：搜「嵌入式实习」时，BOSS 会把「市场实习生」「邀约专员」一起推过来；
    #     只靠 keywords(OR) 挡不住，必须有这一层「必须含核心技能词」的硬门槛。
    must = cfg.get("must_keywords") or []
    if must:
        mhit = [kw for kw in must if _hit(kw, title)]
        if not mhit:
            return False, "非目标方向（职位名不含核心技能词）"

    # 3.6 岗位性质门槛（实习 / 校招 / 应届）—— 职位名 + 卡片标签 合并成一个文本再匹配。
    #     用标签的原因：BOSS 的校招岗很多就写「嵌入式软件工程师」，性质只在
    #     卡片标签里以「在校/应届」体现，光看职位名会整批漏掉。
    entry_kw = cfg.get("entry_keywords") or []
    ehit: list[str] = []
    if entry_kw:
        blob = title + " " + tag_blob
        ehit = [kw for kw in entry_kw if _hit(kw, blob)]
        if not ehit:
            return False, f"非实习/校招岗（标签：{'/'.join(tags) or '无'}）"

    # 4. 薪资窗口：日薪 100-250 元（月薪按「天/月」换算后一并判定）
    ok, why = _salary_ok(salary, cfg)
    if not ok:
        return False, why

    return True, f"命中：{','.join(hit)}｜性质：{','.join(ehit) if entry_kw else '-'}"


def _salary_ok(salary: str, cfg: dict) -> tuple[bool, str]:
    """判断薪资是否落在配置的日薪窗口内。返回 (是否通过, 原因)。

    规则（filter.salary）：
      - daily_min / daily_max：日薪窗口，单位「元/天」
      - day_per_month：月薪→日薪的换算基数（默认 21.75 天/月）
      - 岗位写「元/天」→ 直接按日薪比
      - 岗位写「15-25K」→ 换算成日薪再比（15K ÷ 21.75 ≈ 690 元/天）
      - 读不出数字（如「面议」）或字体码位还原失败 → **放行**，
        绝不因为「读不懂」就误杀岗位。
    """
    sc = (cfg.get("filter", {}) or {}).get("salary") or {}
    # ★ enabled: false → 整段薪资判断跳过（用户要求「取消薪资限制」时的开关）
    if sc.get("enabled") is False:
        return True, ""
    dmin = sc.get("daily_min")
    dmax = sc.get("daily_max")
    if not dmin and not dmax:
        return True, ""

    text = decode_salary(str(salary or ""))
    if has_unknown_pua(text):
        return True, f"薪资含未识别码位→放行（{text!r}）"

    lo, hi = _parse_daily(text)
    unit = "元/天"
    if lo is None:
        mlo, mhi = _parse_monthly_k(text)
        if mlo is None:
            return True, f"薪资无数字→放行（{text or '空'}）"
        dpm = float(sc.get("day_per_month", 21.75)) or 21.75
        lo, hi = mlo * 1000 / dpm, mhi * 1000 / dpm
        unit = "元/天（月薪换算）"

    if dmin and hi < float(dmin):
        return False, f"日薪低于下限：{hi:.0f}{unit}（原：{text}）"
    if dmax and lo > float(dmax):
        return False, f"日薪高于上限：{lo:.0f}{unit}（原：{text}）"
    return True, ""


def _parse_daily(salary: str) -> tuple[int | None, int | None]:
    """从 '150-200元/天' / '200元/天' 里取日薪区间。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*元\s*/?\s*天", salary)
    if m:
        return int(float(m.group(1))), int(float(m.group(2)))
    m = re.search(r"(\d+(?:\.\d+)?)\s*元\s*/?\s*天", salary)
    if m:
        v = int(float(m.group(1)))
        return v, v
    return None, None


def _parse_monthly_k(salary: str) -> tuple[float | None, float | None]:
    """从 '15-25K' / '20K以上' / '8-12K·13薪' 里取月薪区间（单位 K）。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*[Kk]", salary)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*[Kk]", salary)
    if m:
        v = float(m.group(1))
        return v, v
    return None, None


def _parse_min_salary(salary: str) -> int | None:
    """（兼容保留）从 '15-25K' 里提取月薪下限，单位 K。

    注意：传入的必须是【已解码】文本（core.salary.decode_salary）。
    直接喂 DOM 原文会因字体混淆而永远返回 None —— 这正是历史上
    min_salary_k 从未生效的原因。
    """
    lo, _ = _parse_monthly_k(decode_salary(str(salary or "")))
    return None if lo is None else int(lo)
