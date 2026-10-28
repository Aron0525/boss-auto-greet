"""岗位过滤链（五层门槛）。"""
import pytest

from bossgreet.filters.filter import _hit, _salary_ok, _target_cities, matches


# ---------- 关键词命中 ----------

def test_ascii_keyword_uses_word_boundary():
    # 不区分大小写
    assert _hit("AI", "Ai agent 开发") is True
    assert _hit("ai", "AI应用开发") is True
    # 但 "AI" 不能命中 Email / Detail
    assert _hit("AI", "Email 开发") is False
    assert _hit("AI", "Detail 设计") is False
    # "RAG" 不能命中 storage
    assert _hit("RAG", "storage 开发") is False


def test_chinese_keyword_uses_substring():
    assert _hit("嵌入式", "嵌入式软件工程师") is True
    assert _hit("硬件", "硬件实习生") is True
    assert _hit("嵌入式", "销售专员") is False


def test_ascii_word_at_string_edge():
    assert _hit("C", "C 语言开发") is True
    assert _hit("C", "PCB 设计") is False


# ---------- 城市 ----------

def test_target_cities_prefers_list_over_single():
    cfg = {"filter": {"cities": ["深圳", "广州"], "city": "东莞"}}
    assert _target_cities(cfg) == ["深圳", "广州"]


def test_target_cities_falls_back_to_single():
    cfg = {"filter": {"city": "深圳"}}
    assert _target_cities(cfg) == ["深圳"]


# ---------- 完整过滤链 ----------

BASE_CFG = {
    "filter": {"cities": ["深圳"], "city": "深圳",
               "salary": {"enabled": False}},
    "keywords": ["嵌入式", "单片机"],
    "must_keywords": ["嵌入式", "单片机"],
    "entry_keywords": [],
    "exclude_keywords": ["销售", "产品经理"],
    "exclude_tags": ["硕士"],
}


def _job(**kw):
    job = {"title": "嵌入式软件工程师", "company": "某公司",
           "city": "深圳", "salary": "", "tags": []}
    job.update(kw)
    return job


def test_matching_job_passes():
    ok, why = matches(_job(), BASE_CFG)
    assert ok is True
    assert "命中" in why


def test_city_mismatch_rejected():
    ok, why = matches(_job(city="北京"), BASE_CFG)
    assert ok is False
    assert "城市不符" in why


def test_exclude_keyword_wins_over_everything():
    # 即便命中了目标关键词，排除词优先级更高
    ok, why = matches(_job(title="嵌入式产品经理"), BASE_CFG)
    assert ok is False
    assert "排除词" in why


def test_exclude_tag_rejected():
    ok, why = matches(_job(tags=["硕士", "3-5年"]), BASE_CFG)
    assert ok is False
    assert "排除标签" in why


def test_no_keyword_rejected():
    cfg = dict(BASE_CFG, keywords=["销售"])
    ok, why = matches(_job(), cfg)
    assert ok is False
    assert "无匹配关键词" in why


def test_must_keyword_is_and_gate():
    # 场景：搜「嵌入式实习」时 BOSS 会把「市场实习生」一起推过来。
    # keywords 里若也收了「实习生」这种宽泛词（OR），它就放行了 ——
    # 只有 must_keywords 这一层 AND 能锁死方向。
    cfg = dict(BASE_CFG,
               keywords=["嵌入式", "单片机", "实习生"],
               must_keywords=["嵌入式", "单片机"])
    ok, why = matches(_job(title="市场实习生"), cfg)
    assert ok is False
    assert "非目标方向" in why

    # 同一套配置下，方向对的正经岗照常通过
    ok, _ = matches(_job(title="嵌入式实习生"), cfg)
    assert ok is True


def test_entry_keyword_matches_title_plus_tags():
    cfg = dict(BASE_CFG, entry_keywords=["实习", "校招", "应届", "在校"])
    # 标题里没有性质词，但标签里有 → 应通过（BOSS 校招岗的常见写法）
    ok, _ = matches(_job(title="嵌入式软件工程师", tags=["在校/应届", "本科"]), cfg)
    assert ok is True
    # 都没有 → 拦掉
    ok, why = matches(_job(title="嵌入式软件工程师", tags=["1-3年", "本科"]), cfg)
    assert ok is False
    assert "非实习/校招岗" in why


# ---------- 薪资 ----------

def _sal_cfg(enabled=True, dmin=None, dmax=None):
    return {"filter": {"salary": {"enabled": enabled, "daily_min": dmin,
                                  "daily_max": dmax, "day_per_month": 21.75}}}


def test_salary_disabled_lets_everything_through():
    assert _salary_ok("1-2K", _sal_cfg(enabled=False))[0] is True


@pytest.mark.parametrize("text,ok", [
    ("150-200元/天", True),    # 落在 100~250
    ("80元/天", False),        # 低于下限
    ("400元/天", False),       # 高于上限
    ("面议", True),            # 读不出数字 → 放行
])
def test_salary_daily_window(text, ok):
    assert _salary_ok(text, _sal_cfg(True, 100, 250))[0] is ok


def test_salary_monthly_is_converted_by_day_per_month():
    # 15K ÷ 21.75 ≈ 690 元/天 → 高于 250 → 拒
    ok, why = _salary_ok("15-25K", _sal_cfg(True, 100, 250))
    assert ok is False
    assert "月薪换算" in why or "日薪高于上限" in why


def test_salary_unknown_pua_is_released():
    # 字体映射变了 → 宁可放行，绝不误杀
    ok, why = _salary_ok("\ue0ff-\ue0fe元/天", _sal_cfg(True, 100, 250))
    assert ok is True
    assert "放行" in why


def test_salary_encoded_value_is_decoded_before_compare():
    # DOM 里是 PUA 码位，解码后是 150-200元/天 → 通过
    raw = "".join(chr(0xE031 + int(c)) for c in "150")
    ok, _ = _salary_ok(f"{raw}-{raw}元/天", _sal_cfg(True, 100, 250))
    assert ok is True
