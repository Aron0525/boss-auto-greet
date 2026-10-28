# ============ LLM 生成器：让模型读完整 JD 再写这一条 ============
# 走 OpenAI 兼容的 /chat/completions，所以百炼、DeepSeek、OpenAI、本地 vLLM 都能用，
# 只改 base_url + model + key 即可。
#
# 配置见 config.yaml 的 message.llm。
# 密钥只从环境变量读，不写进任何文件。
from __future__ import annotations

import json
import os

# 约束模型的硬规则。这里每一条都是为了防「AI 味」和「编造经历」。
SYSTEM_PROMPT = """你是一位正在 BOSS 直聘上找工作的应届生，现在要给 HR 发第一条打招呼消息。

你的任务：根据【我的资料】和【目标岗位】，写一条 150~280 字的中文招呼语。

硬性规则（违反任何一条都算失败）：
1. 必须结合【目标岗位】里 1~2 个**具体**的要求或关键词（例如某个技术、某类产品、某个职责），
   让 HR 一眼看出这条消息是专门为这个岗位写的，不是群发。
2. 只能使用【我的资料】里明确存在的事实。**绝对不许编造**任何项目、技能、实习、奖项、证书。
   资料里没有的经历，一个字都不能写。
3. 如果岗位要求的东西资料里完全没有，不要硬凑、不要说"我也学过"。
   改为强调资料里与该岗位最接近的能力。
4. 语气平实、像真人打字。不要堆砌敬语，不要写"我非常渴望加入贵公司"这类空洞表态，
   不要用排比句，不要分点罗列。
5. 不要出现方括号、花括号、占位符、markdown 语法。
6. 结尾用一句话表达希望进一步沟通。
7. 只输出消息正文本身。不要解释、不要标题、不要用引号包起来。"""


def build_user_prompt(job: dict, profile: dict) -> str:
    jd = (job.get("jd_text") or "").strip()
    if len(jd) > 2000:
        jd = jd[:2000] + "…"
    parts = [
        "【目标岗位】",
        f"职位名称：{job.get('title', '')}",
        f"公司名称：{job.get('company', '')}",
        f"工作城市：{job.get('city', '')}",
        f"薪资：{job.get('salary', '')}",
    ]
    if jd:
        parts.append(f"岗位描述：\n{jd}")
    else:
        parts.append("岗位描述：（页面上没读到 JD 正文，只能根据职位名称写）")

    parts += [
        "",
        "【我的资料】",
        json.dumps(profile, ensure_ascii=False, indent=2),
    ]
    return "\n".join(parts)


class LLMError(RuntimeError):
    pass


def resolve_api_key(cfg_llm: dict) -> str:
    for name in (cfg_llm.get("api_key_env") or "LLM_API_KEY",
                 "LLM_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        v = os.environ.get(name)
        if v:
            return v
    raise LLMError(
        "没有找到 API 密钥。请设置环境变量（任选其一）：\n"
        "  set LLM_API_KEY=sk-xxxx        （Windows CMD）\n"
        "  $env:LLM_API_KEY=\"sk-xxxx\"     （PowerShell）\n"
        "或把 config.yaml 的 message.generator 改成 template / external。"
    )


def chat(system: str, user: str, cfg_llm: dict) -> str:
    try:
        import requests
    except ImportError as exc:
        raise LLMError("LLM 模式需要安装可选依赖：pip install 'bossgreet[llm]'") from exc
    base = (cfg_llm.get("base_url") or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    model = cfg_llm.get("model") or "qwen-plus"
    timeout = int(cfg_llm.get("timeout_s", 60))
    temperature = float(cfg_llm.get("temperature", 0.7))
    key = resolve_api_key(cfg_llm)

    resp = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": 800,
        },
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise LLMError(f"模型接口返回 HTTP {resp.status_code}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError("模型接口返回结构异常") from e


def compose_message(job: dict, profile: dict, cfg_llm: dict) -> str:
    """给定岗位与资料，返回一条招呼语正文。"""
    text = chat(SYSTEM_PROMPT, build_user_prompt(job, profile), cfg_llm)
    return _clean(text)


def _clean(text: str) -> str:
    t = text.strip()
    # 模型有时会自带引号或"消息："前缀
    for pref in ("消息：", "消息:", "招呼语：", "招呼语:"):
        if t.startswith(pref):
            t = t[len(pref):].strip()
    if len(t) >= 2 and t[0] in "“\"'「" and t[-1] in "”\"'」":
        t = t[1:-1].strip()
    return " ".join(t.split())
