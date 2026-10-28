"""薪资字体混淆解码 —— BOSS 用自定义字体把数字换成私有区码位。

这组测试同时是「映射表有没有失效」的哨兵：BOSS 换字体/换偏移后，
decode_salary 会解出乱码，has_unknown_pua 会返回 True —— 过滤层据此
一律放行（绝不因为读不懂薪资就误杀岗位）。
"""
from bossgreet.core.salary import PUA_HI, PUA_LO, decode_salary, has_unknown_pua


def _enc(digits: str) -> str:
    """把明文数字串还原成 BOSS 的私有区表示（供测试构造输入）。"""
    return "".join(chr(0xE031 + int(c)) for c in digits)


def test_decode_digits_are_linear():
    # 0→U+E031 … 9→U+E03A，逐位偏移
    assert decode_salary(_enc("0123456789")) == "0123456789"


def test_decode_monthly_k_range():
    # 渲染 4-8K
    assert decode_salary(f"{_enc('4')}-{_enc('8')}K") == "4-8K"
    # 渲染 8-11K
    assert decode_salary(f"{_enc('8')}-{_enc('11')}K") == "8-11K"


def test_decode_daily_range():
    # 渲染 150-200元/天
    raw = f"{_enc('150')}-{_enc('200')}元/天"
    assert decode_salary(raw) == "150-200元/天"


def test_decode_passes_through_plain_text():
    assert decode_salary("面议") == "面议"
    assert decode_salary("") == ""


def test_has_unknown_pua_after_decode_is_false():
    decoded = decode_salary(f"{_enc('15')}-{_enc('25')}K")
    assert has_unknown_pua(decoded) is False


def test_unknown_pua_is_detected_before_decoding():
    # 码位落在已知区间外 → 映射可能变了
    assert has_unknown_pua("\ue0ff") is True
    assert has_unknown_pua("15-25K") is False


def test_known_range_constants_are_consistent():
    # 映射区间必须正好覆盖 10 个数字
    assert PUA_HI - PUA_LO == 9
