"""配置加载。

查找顺序（先命中先用）：
  1. 显式传入的路径（命令行 --config / --profile）
  2. 当前工作目录下的 config.yaml / profile.yaml / templates.yaml
  3. 包内自带的默认值（只有消息模板有默认值；配置与档案缺失就直接报错，
     因为那两个是「你自己的东西」，不该由包替你编一套）

不做"包内回退到示例配置"这种事：拿示例配置静默跑起来，会让人以为
配置生效了，实际上投的是别人的岗位方向。缺文件就明确报错并提示怎么拿模板。
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

# 包目录（本文件在 bossgreet/core/ 下）
PKG_ROOT = Path(__file__).resolve().parent.parent

CONFIG_NAME = "config.yaml"
PROFILE_NAME = "profile.yaml"
TEMPLATES_NAME = "templates.yaml"

CONFIG_ENV = "BOSSGREET_CONFIG"
PROFILE_ENV = "BOSSGREET_PROFILE"

# 包内自带的默认消息模板（与个人无关，可安全内置）
_PKG_TEMPLATES = PKG_ROOT / "messaging" / TEMPLATES_NAME

_MISSING_HINT = {
    CONFIG_NAME: "复制一份配置模板：Copy-Item config/config.example.yaml config.yaml",
    PROFILE_NAME: "复制一份档案模板：Copy-Item config/profile.example.yaml profile.yaml",
}


def _load(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"找不到配置文件：{path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"配置文件格式不对（应为 YAML 映射）：{path}")
    return data


def _resolve(explicit: str | Path | None, env_key: str,
             name: str, pkg_default: Path | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        return p if p.is_absolute() else Path.cwd() / p

    env = os.environ.get(env_key, "").strip()
    if env:
        p = Path(env)
        return p if p.is_absolute() else Path.cwd() / p

    cwd = Path.cwd() / name
    if cwd.exists():
        return cwd

    if pkg_default is not None and pkg_default.exists():
        return pkg_default

    hint = _MISSING_HINT.get(name, "")
    raise FileNotFoundError(
        f"找不到 {name}。当前目录：{Path.cwd()}\n"
        f"  → {hint or '请先创建它'}\n"
        f"  → 也可以用 --{name.split('.')[0]} <路径> 显式指定。")


def load_config(path: str | Path | None = None) -> dict:
    """加载运行配置（浏览器 / 搜索词 / 过滤 / 节奏 / 消息生成器）。"""
    return _load(_resolve(path, CONFIG_ENV, CONFIG_NAME))


def load_profile(path: str | Path | None = None) -> dict:
    """加载「我的信息」（用于生成定制招呼语）。"""
    return _load(_resolve(path, PROFILE_ENV, PROFILE_NAME))


def load_templates(path: str | Path | None = None) -> dict:
    """加载消息模板体系。找不到时用包内自带的通用模板。"""
    return _load(_resolve(path, "", TEMPLATES_NAME, _PKG_TEMPLATES))


def state_path(key: str, default_name: str, cfg: dict | None = None) -> Path:
    """取一个状态文件路径（contacted.json / jobs.jsonl 之类）。

    配置里 paths.<key> 的路径优先级最高；相对路径一律按当前工作目录解析，
    这样每个使用者在自己目录下跑就有自己的状态，不会互相覆盖。
    """
    raw = ((cfg or {}).get("paths") or {}).get(key) or default_name
    p = Path(str(raw))
    return p if p.is_absolute() else Path.cwd() / p
