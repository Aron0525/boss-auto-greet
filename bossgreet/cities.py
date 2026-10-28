"""BOSS 城市码表与城市组解析。

为什么要单独成模块：
  城市码是纯数据，却被两处需要 —— 页面对象要用它拼搜索 URL，
  过滤层要用它把「珠三角」展开成逐个城市去搜。放在页面对象里会让
  纯逻辑的过滤/去重反过来依赖浏览器层，单测就没法不连浏览器跑了。

码值取自 BOSS 官方城市表 https://www.zhipin.com/wapi/zpCommon/data/city.json 。
"""
from __future__ import annotations

# BOSS 搜索 URL 的 `city` 参数值。城市名 → 城市码。
CITY_CODES: dict[str, str] = {
    "全国": "100010000",
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "东莞": "101281600",
    "汕头": "101280500",
    "珠海": "101280700",
    "佛山": "101280800",
    "惠州": "101280300",
    "中山": "101281700",
    "江门": "101281100",
    "肇庆": "101280900",
    "苏州": "101190400",
    "成都": "101270100",
    "武汉": "101200100",
    "西安": "101110100",
    "南京": "101190100",
    "长沙": "101250100",
    "天津": "101030100",
    "重庆": "101040100",
    "郑州": "101180100",
    "青岛": "101120200",
    "济南": "101120100",
    "合肥": "101220100",
    "福州": "101230100",
    "厦门": "101230200",
    "昆明": "101290100",
    "大连": "101070200",
    "沈阳": "101070100",
    "哈尔滨": "101050100",
    "长春": "101060100",
    "石家庄": "101090100",
    "南昌": "101240100",
    "贵阳": "101260100",
    "南宁": "101300100",
    "太原": "101100100",
    "兰州": "101160100",
    "无锡": "101190200",
    "宁波": "101210400",
}

# 城市组：一条命令扫一片区域。值 = 允许投递的城市集合。
CITY_GROUPS: dict[str, list[str]] = {
    "珠三角": ["深圳", "广州", "东莞", "佛山", "珠海", "惠州", "中山", "江门", "肇庆"],
    "长三角": ["上海", "杭州", "苏州", "南京", "宁波", "无锡"],
    "京津冀": ["北京", "天津"],
}

# 没在 CITY_CODES 里的城市，BOSS 也能搜（city 参数留空 = 全国），
# 但结果会变成全国混杂。这里标记出来，解析时提醒而不是静默跳过。


def split_city_arg(raw: str) -> list[str]:
    """把 "深圳,广州" / "深圳、广州" / "深圳，广州" 拆成城市列表。"""
    if not raw:
        return []
    return [x.strip() for x in raw.replace("，", ",").replace("、", ",").split(",") if x.strip()]


def searchable(cities: list[str]) -> list[str]:
    """过滤出 BOSS 有城市码的那些（逐个搜索时 URL 才带得准）。"""
    return [c for c in cities if c in CITY_CODES]


def resolve_cities(raw: str, cfg: dict) -> tuple[list[str], list[str]]:
    """把命令行/配置里的城市串解析成 (允许投递的城市集合, 实际逐个搜索的城市列表)。

    为什么要拆成两个列表：
      BOSS 的搜索 URL 一次只能带一个城市码，所以「珠三角」这类区域必须
      拆成多个城市逐个搜；但**过滤**必须用整组城市 —— 搜「广州」时
      BOSS 会把 `佛山·顺德区·北滘` 的岗位一起推上来（广佛同城），
      若过滤只认「广州」，这些本该投的岗位会被「城市不符」整批误杀。
    """
    raw = (raw or "").strip()

    group = CITY_GROUPS.get(raw)
    if group:
        return list(group), (searchable(group) or list(group))

    if raw:
        parts = split_city_arg(raw)
        if len(parts) > 1:
            return parts, (searchable(parts) or parts)
        return parts, parts

    # 命令行没指定：读配置。filter.cities（列表）优先，filter.city（单个）向后兼容。
    f = (cfg or {}).get("filter", {}) or {}
    allow = f.get("cities") or []
    if isinstance(allow, str):
        allow = split_city_arg(allow)
    allow = list(allow)
    single = (f.get("city") or "").strip()
    if single and single not in allow:
        allow.append(single)
    return allow, ([allow[0]] if allow else [""])
