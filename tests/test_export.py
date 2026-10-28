"""投递清单导出。"""
import json

from bossgreet.reporting.export import (
    export_contacted,
    load_history,
    to_html,
    to_markdown,
)


def _sample():
    return [
        {"id": "1", "time": "2026-01-01 10:00:00", "title": "嵌入式软件工程师",
         "company": "甲公司", "city": "深圳", "salary": "150-200元/天",
         "message": "您好", "mode": "greet_only"},
        {"id": "2", "time": "2026-01-01 10:05:00", "title": "嵌入式软件工程师",
         "company": "乙公司", "city": "广州", "salary": "8-12K",
         "message": "您好", "manual": True},
    ]


def test_markdown_has_table_and_totals():
    md = to_markdown(_sample())
    assert "共 2 条" in md
    assert "| # | 时间 |" in md
    assert "甲公司" in md


def test_markdown_empty_is_graceful():
    md = to_markdown([])
    assert "没有记录" in md


def test_html_escapes_content():
    html = to_html([{"title": "<script>alert(1)</script>", "company": "甲|乙"}])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_html_has_rows():
    html = to_html(_sample())
    assert html.count("<tr>") >= 3      # 表头 + 2 条明细


def test_load_history_reads_contacted_file(tmp_path):
    p = tmp_path / "contacted.json"
    p.write_text(json.dumps({"ids": ["1"], "history": _sample()},
                            ensure_ascii=False), encoding="utf-8")
    assert len(load_history(p)) == 2


def test_load_history_missing_file_is_empty(tmp_path):
    assert load_history(tmp_path / "nope.json") == []


def test_load_history_corrupted_is_empty(tmp_path):
    p = tmp_path / "contacted.json"
    p.write_text("not json", encoding="utf-8")
    assert load_history(p) == []


def test_export_writes_files(tmp_path):
    written = export_contacted(_sample(), tmp_path / "清单.md",
                               tmp_path / "清单.html")
    assert len(written) == 2
    assert (tmp_path / "清单.md").exists()
    assert (tmp_path / "清单.html").exists()


def test_export_md_only(tmp_path):
    written = export_contacted(_sample(), tmp_path / "a.md")
    assert len(written) == 1
