"""BOSS 直聘「薪资字体混淆」解码。

背景：BOSS 用自定义字体 `kanzhun-mix` 把数字替换成私有区(PUA)码位，
页面上渲染 `150-200元/天`，DOM 里拿到的却是
`\ue032\ue036\ue031-\ue033\ue031\ue031元/天`。
后果：commands/filter.py 的薪资正则永远匹配不到数字 → min_salary_k 从未生效。

实测映射（2026-10-15 用截图逐条比对验证，4/4 吻合）是【线性】的：

    数字 d  →  U+E031 + d
    '0'→\ue031  '1'→\ue032  '2'→\ue033  '3'→\ue034  '4'→\ue035
    '5'→\ue036  '6'→\ue037  '7'→\ue038  '8'→\ue039  '9'→\ue03a

验证样本：
    \ue035-\ue039K                              渲染 4-8K
    \ue032\ue036\ue031-\ue033\ue031\ue031元/天   渲染 150-200元/天
    \ue039-\ue032\ue032K                        渲染 8-11K
    \ue032\ue033\ue031-\ue032\ue036\ue031元/天   渲染 120-150元/天

⚠️ 若 BOSS 日后改映射（换字体/换偏移），解码结果里会残留 PUA 字符或出现
   不合理数值。`has_unknown_pua()` 就是给过滤层用的兜底开关：
   一旦发现残留，薪资判定一律放行，绝不因为读不懂就误杀岗位。
"""
from __future__ import annotations

# 数字 0 对应的私有区码位
PUA_ZERO = 0xE031
PUA_LO, PUA_HI = 0xE031, 0xE03A


def decode_salary(text: str) -> str:
    """把薪资里的 PUA 码位还原成明文数字，其余字符原样保留。"""
    if not text:
        return ""
    out: list[str] = []
    for ch in str(text):
        cp = ord(ch)
        if PUA_LO <= cp <= PUA_HI:
            out.append(str(cp - PUA_ZERO))
        else:
            out.append(ch)
    return "".join(out)


def has_unknown_pua(text: str) -> bool:
    """文本里是否还有还原不掉的私有区字符（=映射可能变了，别信解码结果）。"""
    return any(0xE000 <= ord(c) <= 0xF8FF for c in str(text or ""))
