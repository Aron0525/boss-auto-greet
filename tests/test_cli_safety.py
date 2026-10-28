"""CLI 安全边界：所有浏览器对象均为模拟对象，不连接网站。"""
from types import SimpleNamespace

import pytest
import yaml

from bossgreet import cli
from bossgreet.messaging.compose import Message


def test_init_creates_local_files_without_overwrite(tmp_path):
    assert cli.main(["init", "--dir", str(tmp_path)]) == 0
    config = tmp_path / "config.yaml"
    profile = tmp_path / "profile.yaml"
    assert config.exists() and profile.exists()
    profile.write_text("basic: {name: local}\n", encoding="utf-8")
    assert cli.main(["init", "--dir", str(tmp_path)]) == 0
    assert profile.read_text(encoding="utf-8") == "basic: {name: local}\n"


def test_run_requires_send_and_confirmation(monkeypatch):
    monkeypatch.setattr(cli, "BrowserSession", lambda *_: pytest.fail("不应连接浏览器"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["run"])
    assert exc.value.code == 2
    monkeypatch.setattr("builtins.input", lambda _: "q")
    assert cli.main(["run", "--send"]) == 1


def test_run_limit_counts_actual_sends_while_read_only_counts_processed_jobs():
    assert not cli._limit_reached("run", attempted=30, sent=29, limit=30)
    assert cli._limit_reached("run", attempted=31, sent=30, limit=30)
    assert cli._limit_reached("preview", attempted=30, sent=0, limit=30)


def test_company_limit_includes_prior_successful_contacts_only():
    history = [
        {"company": "示例科技有限公司", "mode": "greet_only"},
        {"company": "示例科技有限公司"},
        {"company": "示例科技有限公司", "mode": "uncertain"},
        {"company": "另一家公司", "mode": "blocked_profile_mismatch"},
    ]
    hits = cli._company_hits_from_history(history)
    assert hits[cli._company_key("示例科技有限公司")] == 2
    assert cli._company_key("另一家公司") not in hits


def test_profile_mismatch_is_a_distinct_non_send_result(monkeypatch):
    ticks = iter((0.0, 0.0))
    monkeypatch.setattr(cli.time, "time", lambda: next(ticks))

    class FakeDetail:
        def chat_block_open(self):
            return False
        def click_chat_button(self):
            return True
        def greet_dialog_open(self):
            return False
        def profile_mismatch_open(self):
            return True

    assert cli._click_chat_with_block(FakeDetail()) == "profile_mismatch"


def test_first_start_opens_login_and_stops_before_any_page_action(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    profile = tmp_path / "profile.yaml"
    cfg.write_text("browser:\n  cdp_url: http://127.0.0.1:9222\n", encoding="utf-8")
    profile.write_text("name: 示例用户\n", encoding="utf-8")
    monkeypatch.setattr(cli, "ensure_browser", lambda *_: True)
    monkeypatch.setattr(cli, "ensure_jobs_page", lambda *_: pytest.fail("首次启动不得打开职位页"))
    monkeypatch.setattr(cli, "BrowserSession", lambda *_: pytest.fail("首次启动不得附着页面"))

    assert cli.main(["preview", "--config", str(cfg), "--profile", str(profile)]) == 0
    output = capsys.readouterr().out
    assert "登录页" in output
    assert "没有读取岗位" in output


def test_preview_pipeline_never_calls_send(monkeypatch, tmp_path):
    cli.main(["init", "--dir", str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    job = {"title": "虚构测试岗位", "company": "虚构公司", "city": "测试市", "link": "?jobId=fake1"}

    class FakeCdp:
        def title(self):
            return "模拟岗位页"

    cdp = FakeCdp()

    class FakeSession:
        def __init__(self, *_):
            pass
        def connect(self):
            return cdp
        def browser_name(self):
            return "模拟浏览器"
        def all_targets(self):
            return []
        def disconnect(self):
            pass

    class FakeSearch:
        def __init__(self, *_):
            pass
        def on_target_site(self):
            return True
        def login_hint(self):
            return False
        def open_search(self, *_):
            return True
        def title(self):
            return "模拟搜索"
        def card_count(self):
            return 1
        def job_cards(self):
            return [job]
        def open_card(self, *_):
            return True
        def is_refreshed(self):
            return False

    class FakeDetail:
        def __init__(self, *_):
            pass
        def greet_dialog_open(self):
            return False
        def chat_block_open(self):
            return False
        def has_login_wall(self):
            return False
        def read_jd_text(self):
            return "模拟岗位描述"
        def click_chat_button(self):
            pytest.fail("预览不得点击发送按钮")

    monkeypatch.setattr(cli, "BrowserSession", FakeSession)
    monkeypatch.setattr(cli, "ensure_browser", lambda *_: False)
    monkeypatch.setattr(cli, "ensure_jobs_page", lambda *_: False)
    monkeypatch.setattr(cli, "SearchPage", FakeSearch)
    monkeypatch.setattr(cli, "JobDetailPage", FakeDetail)
    monkeypatch.setattr(cli, "set_input_mode", lambda mode: mode)
    monkeypatch.setattr(cli, "focus_this", lambda *_: True)
    monkeypatch.setattr(cli.bg, "window_visibility", lambda *_: {"visibility": "visible", "hidden": False})
    monkeypatch.setattr(cli, "_resolve_cities", lambda *_: (["测试市"], ["测试市"]))
    monkeypatch.setattr(cli, "_resolve_detail", lambda *_: (cdp, False))
    monkeypatch.setattr(cli, "matches", lambda *_: (True, ""))
    monkeypatch.setattr(cli, "make_provider", lambda *_args, **_kw: SimpleNamespace(
        describe=lambda: "模拟消息", needs_jd=False,
        compose=lambda *_: Message(text="模拟消息", family="test")))

    assert cli.main(["preview", "--custom", "--limit", "1", "--pages", "1", "--show-window"]) == 0
    assert not (tmp_path / "contacted.json").exists()


def test_mock_run_honors_batch_confirmation_and_dedup(monkeypatch, tmp_path):
    """模拟两条岗位；只准第一条发送，第二批输入 q 后停止。"""
    cli.main(["init", "--dir", str(tmp_path)])
    cfg_path = tmp_path / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["search_queries"] = ["模拟搜索"]
    cfg["pace"]["batch_size"] = 1
    cfg["pace"]["daily_limit"] = 3
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    jobs = [
        {"title": f"虚构岗位{i}", "company": "虚构公司", "city": "测试市", "link": f"?jobId=fake{i}"}
        for i in (1, 2)
    ]
    clicks = []
    answers = iter(["send", "q"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    class FakeCdp:
        def title(self):
            return "模拟岗位页"

    cdp = FakeCdp()

    class FakeSession:
        def __init__(self, *_):
            pass
        def connect(self):
            return cdp
        def browser_name(self):
            return "模拟浏览器"
        def all_targets(self):
            return []
        def disconnect(self):
            pass

    class FakeSearch:
        def __init__(self, *_):
            pass
        def on_target_site(self):
            return True
        def login_hint(self):
            return False
        def open_search(self, *_):
            return True
        def title(self):
            return "模拟搜索"
        def card_count(self):
            return len(jobs)
        def job_cards(self):
            return jobs
        def open_card(self, *_):
            return True
        def is_refreshed(self):
            return False

    class FakeDetail:
        CHAT_SELECTORS = "模拟选择器"
        def __init__(self, *_):
            pass
        def _js_find_visible(self, *_):
            return "模拟元素"
        def greet_dialog_open(self):
            return bool(clicks)
        def dismiss_greet_dialog(self):
            return True
        def chat_block_open(self):
            return False
        def has_login_wall(self):
            return False
        def read_jd_text(self):
            return "模拟岗位描述"
        def chat_button_text(self):
            return "继续沟通" if clicks else "立即沟通"
        def click_chat_button(self):
            clicks.append(1)
            return True

    monkeypatch.setattr(cli, "BrowserSession", FakeSession)
    monkeypatch.setattr(cli, "ensure_browser", lambda *_: False)
    monkeypatch.setattr(cli, "ensure_jobs_page", lambda *_: False)
    monkeypatch.setattr(cli, "SearchPage", FakeSearch)
    monkeypatch.setattr(cli, "JobDetailPage", FakeDetail)
    monkeypatch.setattr(cli, "set_input_mode", lambda mode: mode)
    monkeypatch.setattr(cli, "focus_this", lambda *_: True)
    monkeypatch.setattr(cli.bg, "window_visibility", lambda *_: {"visibility": "visible", "hidden": False})
    monkeypatch.setattr(cli, "_resolve_cities", lambda *_: (["测试市"], ["测试市"]))
    monkeypatch.setattr(cli, "_resolve_detail", lambda *_: (cdp, False))
    monkeypatch.setattr(cli, "matches", lambda *_: (True, ""))
    monkeypatch.setattr(cli, "element_exists", lambda *_: True)
    monkeypatch.setattr(cli, "human_delay", lambda *_: None)

    assert cli.main(["run", "--send", "--limit", "2", "--pages", "1", "--show-window"]) == 1
    assert len(clicks) == 1
    assert cli.ContactLog(tmp_path / "contacted.json").keys() == {"fake1"}


def test_new_browser_stays_visible_for_first_login(monkeypatch, tmp_path):
    cli.main(["init", "--dir", str(tmp_path)])
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(cli, "ensure_browser", lambda *_: True)
    monkeypatch.setattr(cli, "ensure_jobs_page", lambda *_: pytest.fail("首次登录不得打开职位页"))
    monkeypatch.setattr(cli, "BrowserSession", lambda *_: pytest.fail("首次登录不得附着页面"))
    monkeypatch.setattr(cli.winpos, "park", lambda *_: pytest.fail("首次登录窗口不得移到屏幕外"))
    assert cli.main(["preview", "--limit", "1"]) == 0
