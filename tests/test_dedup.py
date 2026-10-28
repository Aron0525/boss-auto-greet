"""已联系记录：防重复 + 留痕。"""
import json
import pytest

from bossgreet.filters.dedup import ContactLog, job_key


def test_job_key_prefers_job_id_from_link():
    job = {"link": "https://x/job_detail/?jobId=abc123&x=1",
           "title": "嵌入式软件工程师", "company": "A"}
    assert job_key(job) == "abc123"


def test_job_key_falls_back_to_whole_link():
    job = {"link": "https://x/job_detail/xyz", "title": "t", "company": "c"}
    assert job_key(job) == "https://x/job_detail/xyz"


def test_job_key_falls_back_to_company_and_title():
    assert job_key({"company": "星河", "title": "嵌入式"}) == "星河|嵌入式"


def test_log_starts_empty(tmp_path):
    log = ContactLog(tmp_path / "contacted.json")
    assert log.keys() == set()
    assert log.count() == 0


def test_mark_then_keys(tmp_path):
    log = ContactLog(tmp_path / "contacted.json")
    job = {"title": "嵌入式软件工程师", "company": "A", "city": "深圳",
           "salary": "150-200元/天", "link": "?jobId=1"}
    log.mark("1", job, "您好，我是…", extra={"mode": "greet_only"})
    assert "1" in log.keys()
    assert log.count() == 1
    entry = log.history()[0]
    assert entry["title"] == "嵌入式软件工程师"
    assert entry["message"] == "您好，我是…"
    assert entry["mode"] == "greet_only"


def test_ids_not_duplicated_but_history_appends(tmp_path):
    log = ContactLog(tmp_path / "contacted.json")
    job = {"title": "t", "company": "c"}
    log.mark("1", job, "第一条")
    log.mark("1", job, "第二条")
    assert log.keys() == {"1"}
    assert log.count() == 2


def test_persists_across_instances(tmp_path):
    path = tmp_path / "contacted.json"
    ContactLog(path).mark("1", {"title": "t"}, "hello")
    # 新实例要能读到同一份
    assert "1" in ContactLog(path).keys()


def test_corrupted_file_stops_before_duplicate_send(tmp_path):
    path = tmp_path / "contacted.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    log = ContactLog(path)
    with pytest.raises(ValueError, match="已联系记录无法读取"):
        log.keys()
    with pytest.raises(ValueError):
        log.mark("1", {"title": "t"}, "x")
    assert path.read_text(encoding="utf-8") == "{ 这不是 json"


def test_writes_atomically_without_leftover_tmp(tmp_path):
    path = tmp_path / "contacted.json"
    ContactLog(path).mark("1", {"title": "t"}, "x")
    assert not path.with_name(path.name + ".tmp").exists()
    assert json.loads(path.read_text(encoding="utf-8"))["ids"] == ["1"]


def test_missing_ids_cannot_silently_lose_dedup(tmp_path):
    path = tmp_path / "contacted.json"
    path.write_text('{"history": [{"id": "fake"}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="缺少必要字段"):
        ContactLog(path).keys()
