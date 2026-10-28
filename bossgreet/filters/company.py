"""公司名归一化 —— 只服务于「单公司投递上限」的计数。

下文中的公司名称均为虚构示例。

同一家公司在 BOSS 上会以多种名义出现：「星河」「星河技术有限公司」
「星河云计算」「上海星河技术有限公司」…… 直接按原文计数，上限等于没设。
这里只做两件事：剥掉前缀城市名、剥掉后缀的组织形式词。

⚠ 这是【启发式】规则，不是工商登记解析：目的是把同一家 HR 团队的不同
   岗位归并计数，不追求法律实体级别的精确。
"""
from __future__ import annotations

_CITY_PREFIXES = (
    "上海", "北京", "深圳", "广州", "东莞", "佛山", "珠海", "惠州", "中山",
    "江门", "肇庆", "杭州", "苏州", "成都", "武汉", "南京", "西安", "长沙",
    "广东", "中国",
)

# 后缀按【长在前】排列，先剥长的，避免「有限公司」把「股份」剩下来。
_COMPANY_SUFFIXES = (
    "股份有限公司", "有限责任公司", "技术有限公司", "科技有限公司",
    "有限公司", "集团公司", "控股", "集团",
    "云计算", "科技", "技术", "信息", "网络", "数字", "软件", "智能",
)


def normalize_company_key(name: str | None) -> str:
    """把公司名压成一个用于计数的 key。

    例：「上海星河技术有限公司」→「星河」、「星河云计算」→「星河」。
    剥到只剩两个字以下时就停手 —— 再剥会把「星河」变成「华」。
    """
    s = (name or "").strip()
    if not s:
        return ""
    for p in _CITY_PREFIXES:
        if s.startswith(p) and len(s) > len(p):
            s = s[len(p):]
            break

    changed = True
    while changed:
        changed = False
        for suf in _COMPANY_SUFFIXES:
            # 至少留两个字，避免把公司名削没了
            if s.endswith(suf) and len(s) - len(suf) >= 2:
                s = s[: -len(suf)]
                changed = True
                break
    return s


def skip_company_keys(names: list[str] | None) -> set[str]:
    """配置里的公司黑名单 → 归一化后的 key 集合（写「青禾」可命中「广东青禾科技有限公司」）。"""
    return {normalize_company_key(c) for c in (names or []) if (c or "").strip()}
