"""已联系记录：防重复投递 + 留痕（记下当时发出去的原话）。

每次成功打招呼/发消息都追加一条，事后可回看「我给这家到底发了什么」。

路径策略（相对于调用时的**当前工作目录**，不是包目录）：
  每个使用者在自己的目录下跑，就有一份自己的记录，不会和别人串。
  想改位置就配置 paths.contacted，或设环境变量 BOSSGREET_CONTACTED。
  该文件默认 gitignore —— 里面有你的真实投递历史，别提交。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

DEFAULT_NAME = "contacted.json"
ENV_KEY = "BOSSGREET_CONTACTED"


def _empty() -> dict:
    return {"ids": [], "history": []}


class ContactLog:
    """一份已联系记录。构造时给路径，之后所有读写都走它。"""

    def __init__(self, path: str | Path | None = None):
        p = Path(path or os.environ.get(ENV_KEY) or DEFAULT_NAME)
        self.path = p if p.is_absolute() else Path.cwd() / p

    def _read(self) -> dict:
        if not self.path.exists():
            return _empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"已联系记录无法读取，请先修复或备份：{self.path}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"已联系记录格式不正确：{self.path}")
        if "ids" not in data or "history" not in data:
            raise ValueError(f"已联系记录缺少必要字段：{self.path}")
        if not isinstance(data.get("ids", []), list) or not isinstance(data.get("history", []), list):
            raise ValueError(f"已联系记录格式不正确：{self.path}")
        return data

    def keys(self) -> set[str]:
        """已联系过的岗位唯一标识集合。"""
        return set(self._read().get("ids", []))

    def history(self) -> list[dict]:
        return self._read().get("history", [])

    def count(self) -> int:
        return len(self._read().get("history", []))

    def mark(self, job_id: str, job: dict, message: str, extra: dict | None = None):
        """记录一次成功的联系（写盘）。"""
        data = self._read()
        if job_id not in data["ids"]:
            data["ids"].append(job_id)
        entry = {
            "id": job_id,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "salary": job.get("salary", ""),
            "city": job.get("city", ""),
            "link": job.get("link", ""),
            "message": message,
        }
        if extra:
            entry.update(extra)
        data["history"].append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换：中途崩了也不会把旧记录毁掉
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


# ---------- 模块级默认实例（函数式用法继续可用）----------

_default: ContactLog | None = None


def get_log(path: str | Path | None = None) -> ContactLog:
    """拿到记录对象（默认模块级单例）。传了 path 就切到那个文件。"""
    global _default
    if path is not None or _default is None:
        _default = ContactLog(path)
    return _default


def set_log_path(path: str | Path) -> ContactLog:
    """把后续所有记录操作切到指定文件。"""
    return get_log(path)


def load_contacted() -> set[str]:
    return get_log().keys()


def mark_contacted(job_id: str, job: dict, message: str, extra: dict | None = None):
    get_log().mark(job_id, job, message, extra)


_JOBID_RE = re.compile(r"jobId=([A-Za-z0-9_-]+)")


def job_key(job: dict) -> str:
    """岗位唯一标识：优先链接里的 jobId，其次整条链接，最后退回 公司|职位。"""
    link = (job.get("link") or "").strip()
    if link:
        m = _JOBID_RE.search(link)
        if m:
            return m.group(1)
        return link
    return f"{job.get('company', '')}|{job.get('title', '')}"
