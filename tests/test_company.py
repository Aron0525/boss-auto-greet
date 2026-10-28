"""公司名归一化 —— 测试公司名称均为虚构。"""
from bossgreet.filters.company import normalize_company_key, skip_company_keys


def test_strip_city_prefix():
    assert normalize_company_key("上海星河技术有限公司") == "星河"


def test_strip_org_suffixes():
    assert normalize_company_key("星河技术有限公司") == "星河"
    assert normalize_company_key("星河云计算") == "星河"
    assert normalize_company_key("某某股份有限公司") == "某某"


def test_plain_name_unchanged():
    assert normalize_company_key("星河") == "星河"


def test_empty_and_none():
    assert normalize_company_key("") == ""
    assert normalize_company_key(None) == ""


def test_short_name_is_not_over_stripped():
    # 剥到只剩两个字以下就停手，避免把「星河」削成「华」
    assert normalize_company_key("青苗科技") == "青苗"
    assert normalize_company_key("青苗") == "青苗"


def test_blank_list_gives_empty_set():
    assert skip_company_keys([]) == set()
    assert skip_company_keys(None) == set()
    assert skip_company_keys(["青禾"]) == {"青禾"}


def test_blacklist_matches_normalized_form():
    # 配置里写「青禾」，要能命中「广东青禾科技有限公司」
    keys = skip_company_keys(["青禾"])
    assert normalize_company_key("广东青禾科技有限公司") in keys
