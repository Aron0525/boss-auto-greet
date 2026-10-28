"""城市组解析与搜索 URL 构建。"""
from bossgreet.cities import (
    CITY_CODES,
    CITY_GROUPS,
    resolve_cities,
    searchable,
    split_city_arg,
)
from bossgreet.pages.zhipin import SearchPage


def test_city_arg_splitting():
    assert split_city_arg("深圳,广州") == ["深圳", "广州"]
    assert split_city_arg("深圳、广州") == ["深圳", "广州"]
    assert split_city_arg("深圳，广州") == ["深圳", "广州"]
    assert split_city_arg("") == []


def test_group_expands_to_search_list_and_allow_list():
    allow, to_search = resolve_cities("珠三角", {})
    assert "佛山" in allow
    # 组内每个城市都要有码，否则 URL 拼不出城市 → 会被静默跳过
    assert set(to_search) == set(CITY_GROUPS["珠三角"])
    for c in CITY_CODES:
        pass
    assert all(c in CITY_CODES for c in to_search)


def test_single_city_passes_through():
    allow, to_search = resolve_cities("深圳", {})
    assert allow == ["深圳"]
    assert to_search == ["深圳"]


def test_multiple_cities_are_searched_only_if_has_code():
    allow, to_search = resolve_cities("深圳,广州", {})
    assert allow == ["深圳", "广州"]
    assert to_search == ["深圳", "广州"]


def test_config_fallback():
    cfg = {"filter": {"cities": ["东莞", "佛山"]}}
    allow, to_search = resolve_cities("", cfg)
    assert allow == ["东莞", "佛山"]
    # 默认一次只搜一个城市（逐个跑），但整组都允许投递
    assert to_search == ["东莞"]


def test_config_single_city_backward_compatible():
    cfg = {"filter": {"city": "珠海"}}
    allow, to_search = resolve_cities("", cfg)
    assert allow == ["珠海"]
    assert to_search == ["珠海"]


def test_searchable_filters_unknown():
    assert searchable(["深圳", "不存在的城"]) == ["深圳"]


# ---------- URL 构建 ----------

def test_build_search_url_with_city_and_query():
    url = SearchPage.build_search_url("深圳", "嵌入式 实习")
    assert url.startswith(SearchPage.HOME + "?")
    assert "city=101280600" in url
    assert "query=" in url


def test_build_search_url_without_city():
    url = SearchPage.build_search_url("", "嵌入式")
    assert "city=" not in url


def test_query_is_url_encoded():
    url = SearchPage.build_search_url("深圳", "嵌入式 实习")
    # 空格必须被编码，否则 URL 会被截断
    assert " " not in url.split("?", 1)[1]
    assert "%20" in url


def test_page_greater_than_one_adds_page_param():
    assert "page=2" in SearchPage.build_search_url("深圳", "a", 2)
    assert "page=" not in SearchPage.build_search_url("深圳", "a", 1)
