# ============ 生成器抽象：同一条流水线，换不同的"写消息的人" ============
#   template  —— 本地模板 + 槽位（零成本、零延迟、结果可预测）
#   llm       —— 模型读完整 JD 后逐条写（真正逐岗定制，需要 API Key）
#   external  —— 由 Agent/人工预生成 messages.json，脚本只负责发送
#
# 三者对外的接口完全一致：provider.compose(job) -> Message
from __future__ import annotations

import json
from pathlib import Path

from bossgreet.messaging.compose import Composer, Message, validate_text
from bossgreet.messaging.llm import LLMError, compose_message


class BaseProvider:
    name = "base"
    needs_jd = False          # 是否必须读到 JD 正文才有意义

    def compose(self, job: dict) -> Message:      # pragma: no cover - 抽象
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class TemplateProvider(BaseProvider):
    """本地模板 + 槽位。永不失败，作为其他两者的兜底。"""

    name = "template"
    needs_jd = False

    def __init__(self, profile: dict, templates: dict, seed: int | None = None):
        self._comp = Composer(profile, templates, seed=seed)
        self._settings = templates.get("settings", {}) or {}

    def compose(self, job: dict) -> Message:
        msg = self._comp.compose(job)
        return msg

    def describe(self) -> str:
        return "本地模板 + 槽位"


class LLMProvider(BaseProvider):
    """模型逐条写。失败时回退模板，并附告警供发送入口停批。"""

    name = "llm"
    needs_jd = True

    def __init__(self, profile: dict, templates: dict, cfg_llm: dict, seed: int | None = None):
        self._profile = profile
        self._cfg = cfg_llm or {}
        self._settings = templates.get("settings", {}) or {}
        self._fallback = TemplateProvider(profile, templates, seed=seed)

    def compose(self, job: dict) -> Message:
        try:
            text = compose_message(job, self._profile, self._cfg)
        except (LLMError, Exception) as e:      # 网络/限流/结构异常都回退
            msg = self._fallback.compose(job)
            msg.warnings.append(f"LLM 失败已回退模板（{type(e).__name__}）")
            return msg

        max_chars = int(self._settings.get("max_chars", 300))
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        warns = validate_text(text, job, self._settings)
        return Message(text=text, family="llm", project_id="", hooks=[], warnings=warns)

    def describe(self) -> str:
        return f"LLM 逐岗生成（{self._cfg.get('model', '?')}）"


class FixedProvider(BaseProvider):
    """固定文案：所有岗位发同一条消息，逐字发送、不加工、不截断、不回退。

    场景：用户手写了一段自我介绍，要求「每个 AI 岗位都要发给 HR」。
    和 external 的区别：external 要按「公司|职位」逐个键匹配，没命中就悄悄
    回退成模板；fixed 只有一个文案，天然全员一致，也不会发错内容。
    """

    name = "fixed"
    needs_jd = False

    def __init__(self, profile: dict, templates: dict, text: str, seed: int | None = None):
        self._text = str(text or "").strip()
        self._settings = templates.get("settings", {}) or {}

    def compose(self, job: dict) -> Message:
        # 只跑长度告警，不做「必须含公司名/职位名」那类检查 ——
        # 固定文案本来就不可能逐岗提到公司，那是群发式自我介绍的正常形态。
        warns: list[str] = []
        max_chars = int(self._settings.get("max_chars", 300))
        if len(self._text) > max_chars:
            warns.append(f"超长（{len(self._text)} > {max_chars}）——已按原文发送，未裁剪")
        return Message(text=self._text, family="fixed", warnings=warns)

    def describe(self) -> str:
        return f"固定文案（{len(self._text)} 字，所有岗位同一句）"


class ExternalProvider(BaseProvider):
    """读取预生成的 messages.json 并原样发送。

    文件格式（键 = jobId 或 "公司|职位"，值为消息正文）：
        {
          "8a9b...": "您好，我是…",
          "星河|嵌入式软件工程师": "您好，我是…"
        }
    """

    name = "external"
    needs_jd = False

    def __init__(self, profile: dict, templates: dict, path: str | Path,
                 seed: int | None = None):
        self._path = Path(path)
        self._settings = templates.get("settings", {}) or {}
        self._fallback = TemplateProvider(profile, templates, seed=seed)
        self._map: dict[str, str] = {}
        self._load()

    def _load(self):
        if not self._path.exists():
            raise FileNotFoundError(
                f"external 模式需要消息文件，但没找到：{self._path}\n"
                f"先用 --capture 抓岗位，再生成 messages.json。"
            )
        data = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{self._path} 应该是「键 → 消息正文」的 JSON 对象")
        self._map = {str(k): str(v) for k, v in data.items()}

    def compose(self, job: dict) -> Message:
        from bossgreet.filters.dedup import job_key
        keys = [
            job_key(job),
            f"{job.get('company', '')}|{job.get('title', '')}",
        ]
        for k in keys:
            if k and k in self._map:
                text = self._map[k].strip()
                warns = validate_text(text, job, self._settings)
                return Message(text=text, family="external", warnings=warns)
        msg = self._fallback.compose(job)
        msg.warnings.append("messages.json 里没有这个岗位，已回退模板")
        return msg

    def describe(self) -> str:
        return f"预生成 messages.json（已载入 {len(self._map)} 条）"


def make_provider(cfg: dict, profile: dict, templates: dict,
                  messages_path: str | Path | None = None,
                  seed: int | None = None) -> BaseProvider:
    """根据配置选择生成器；无效配置明确报错。"""
    mcfg = cfg.get("message", {}) or {}
    mode = (mcfg.get("generator") or "template").lower()

    if mode == "fixed":
        text = (mcfg.get("fixed_text") or "").strip()
        fpath = mcfg.get("fixed_file")
        if not text and fpath:
            p = Path(fpath)
            if not p.is_absolute():
                p = Path.cwd() / p
            if p.exists():
                text = p.read_text(encoding="utf-8").strip()
            else:
                print(f"[!] fixed 模式指定的文件不存在：{p}")
        if not text:
            raise ValueError("fixed 模式需要 message.fixed_text 或 message.fixed_file")
        return FixedProvider(profile, templates, text, seed=seed)

    if mode == "external":
        path = messages_path or mcfg.get("messages_file") or "messages.json"
        return ExternalProvider(profile, templates, path, seed=seed)

    if mode == "llm":
        return LLMProvider(profile, templates, mcfg.get("llm", {}) or {}, seed=seed)

    if mode != "template":
        raise ValueError(f"不支持的消息生成器：{mode}")
    return TemplateProvider(profile, templates, seed=seed)
