"""把投递记录导出成 Markdown / HTML 清单。

原项目里这件事散在两个一次性脚本里（一个生成 md、一个生成 html，且文件名
带批次信息），这里合并成一个可复用的函数：读 contacted.json → 出清单。
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path


def load_history(path: str | Path) -> list[dict]:
    """读 contacted.json 里的 history 列表。文件不存在/损坏时返回空列表。"""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    return [e for e in (data.get("history") or []) if isinstance(e, dict)]


def _fmt_badge(mode: str) -> str:
    return {
        "greet_only": "仅打招呼",
    }.get(mode, mode or "")


def to_markdown(history: list[dict], title: str = "投递清单") -> str:
    """生成 Markdown 清单。"""
    lines = [f"# {title}", ""]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines.append(f"导出时间：{stamp}　共 {len(history)} 条")
    lines.append("")
    if not history:
        lines.append("（没有记录）")
        return "\n".join(lines) + "\n"

    # 公司维度汇总：一眼看出有没有把同一家投太多
    by_company: dict[str, int] = {}
    for e in history:
        c = (e.get("company") or "").strip() or "-"
        by_company[c] = by_company.get(c, 0) + 1
    top = sorted(by_company.items(), key=lambda kv: -kv[1])[:10]
    lines.append("## 公司分布（前 10）")
    lines.append("")
    lines.append("| 公司 | 条数 |")
    lines.append("|---|---:|")
    for c, n in top:
        lines.append(f"| {c} | {n} |")
    lines.append("")

    lines.append("## 明细")
    lines.append("")
    lines.append("| # | 时间 | 职位 | 公司 | 城市 | 薪资 | 方式 |")
    lines.append("|---|---|---|---|---|---|---|")
    for i, e in enumerate(history, 1):
        mode = _fmt_badge(str((e.get("mode") or "")))
        if e.get("manual"):
            mode = "手动确认"
        cells = [
            str(i),
            str(e.get("time") or ""),
            str(e.get("title") or ""),
            str(e.get("company") or ""),
            str(e.get("city") or ""),
            str(e.get("salary") or ""),
            mode,
        ]
        # 管道符会撑破表格
        cells = [c.replace("|", "/") for c in cells]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def to_html(history: list[dict], title: str = "投递清单") -> str:
    """生成单文件 HTML 清单（自带样式，双击即可看）。"""
    esc = html.escape
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []
    for i, e in enumerate(history, 1):
        mode = _fmt_badge(str(e.get("mode") or ""))
        if e.get("manual"):
            mode = "手动确认"
        rows.append(
            f"<tr><td>{i}</td><td>{esc(str(e.get('time') or ''))}</td>"
            f"<td>{esc(str(e.get('title') or ''))}</td>"
            f"<td>{esc(str(e.get('company') or ''))}</td>"
            f"<td>{esc(str(e.get('city') or ''))}</td>"
            f"<td>{esc(str(e.get('salary') or ''))}</td>"
            f"<td>{esc(mode)}</td></tr>")

    by_company: dict[str, int] = {}
    for e in history:
        c = (e.get("company") or "").strip() or "-"
        by_company[c] = by_company.get(c, 0) + 1
    top = sorted(by_company.items(), key=lambda kv: -kv[1])[:10]
    tops = "".join(
        f"<tr><td>{esc(c)}</td><td class=n>{n}</td></tr>" for c, n in top)

    return f"""<!doctype html>
<meta charset="utf-8">
<title>{esc(title)}</title>
<style>
:root {{ color-scheme: light; }}
body {{ margin:0; padding:32px; background:#f6f7f9; color:#1c2024;
        font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
.wrap {{ max-width:960px; margin:0 auto; }}
h1 {{ font-size:22px; margin:0 0 6px; }}
.sub {{ color:#6b7280; font-size:13px; margin-bottom:24px; }}
.panel {{ background:#fff; border:1px solid #e5e7eb; border-radius:10px;
          padding:20px 22px; margin-bottom:20px; }}
h2 {{ font-size:16px; margin:0 0 12px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #eef0f3; }}
th {{ color:#6b7280; font-weight:600; }}
td.n {{ text-align:right; }}
tbody tr:hover {{ background:#fafbfc; }}
.empty {{ color:#6b7280; }}
</style>
<div class=wrap>
  <h1>{esc(title)}</h1>
  <div class=sub>导出时间 {stamp}　共 {len(history)} 条</div>
  <div class=panel>
    <h2>公司分布（前 10）</h2>
    <table><thead><tr><th>公司</th><th class=n>条数</th></tr></thead>
    <tbody>{tops or '<tr><td colspan=2 class=empty>暂无</td></tr>'}</tbody></table>
  </div>
  <div class=panel>
    <h2>明细</h2>
    <table><thead><tr><th>#</th><th>时间</th><th>职位</th><th>公司</th>
      <th>城市</th><th>薪资</th><th>方式</th></tr></thead>
    <tbody>{"".join(rows) or '<tr><td colspan=7 class=empty>没有记录</td></tr>'}</tbody></table>
  </div>
</div>
"""


def export_contacted(history: list[dict], out_md: str | Path,
                     out_html: str | Path | None = None,
                     title: str = "投递清单") -> list[Path]:
    """把记录写成 md（+ 可选 html），返回实际写出的文件列表。"""
    written: list[Path] = []
    p_md = Path(out_md)
    p_md.parent.mkdir(parents=True, exist_ok=True)
    p_md.write_text(to_markdown(history, title), encoding="utf-8")
    written.append(p_md)
    if out_html:
        p_html = Path(out_html)
        p_html.parent.mkdir(parents=True, exist_ok=True)
        p_html.write_text(to_html(history, title), encoding="utf-8")
        written.append(p_html)
    return written
