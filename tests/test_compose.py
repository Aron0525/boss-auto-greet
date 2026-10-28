"""模板消息生成与质检；以下人物、公司和项目数据均为虚构。"""
import re

from bossgreet.messaging.compose import Composer, validate_text

MINI_TEMPLATES = {
    "settings": {"min_chars": 10, "max_chars": 200, "must_contain_any": ["company", "title"],
                 "max_tech_hooks": 2, "max_skills": 3, "allow_traits": True},
    "families": [
        {"id": "embedded_sw", "match": ["嵌入式软件", "固件"], "positioning_key": "embedded_sw"},
        {"id": "general", "match": [], "positioning_key": "general"},
    ],
    "openings": {
        "embedded_sw": ["您好，我是{positioning}，看到{company_or_guisi}在招「{title}」"],
        "general": ["您好，我是{positioning}，看到{company_or_guisi}在招「{title}」"],
    },
    "tech_hooks": [
        {"tags": ["FreeRTOS", "RTOS"],
         "text": "接触过 RTOS，理解任务划分和同步机制"},
        {"tags": ["CAN"], "text": "用过 CAN 总线"},
        {"tags": ["Linux"], "text": "用过 Linux 环境开发"},
    ],
    "project": {"oneliner": "{project_oneliner}", "highlight": "其中{project_highlight}"},
    "skills": {"lead_in": "平时主要用{skills}"},
    "traits": {"lead_in": "{trait}"},
    "closings": ["期待有机会进一步沟通，谢谢！"],
    "assembly": {"separator": "。",
                 "order": ["opening", "project", "highlight", "hooks", "skills",
                           "traits", "closing"]},
}

MINI_PROFILE = {
    "basic": {"name": "", "education": "示例学历", "school": "",
              "city": "示例市", "availability": ""},
    "positioning": {"embedded_sw": "嵌入式软件方向的应届生",
                    "general": "电子信息类专业应届生"},
    "projects": [
        {"id": "p1", "name": "虚构项目甲", "oneliner": "虚构的 STM32 项目经历",
         "tags": ["STM32", "单片机", "PID"], "highlights": ["虚构的调试记录"]},
        {"id": "p2", "name": "虚构项目乙", "oneliner": "虚构的 ESP32 项目经历",
         "tags": ["ESP32", "语音", "WiFi"], "highlights": ["虚构的开发记录"]},
    ],
    "skills": [
        {"name": "C 语言", "tags": ["C", "嵌入式", "固件"]},
        {"name": "Python", "tags": ["Python", "脚本"]},
    ],
    "traits": ["遇到问题习惯从现象查到具体信号"],
}


def _job(**kw):
    job = {"title": "嵌入式软件工程师", "company": "某某科技",
           "city": "深圳", "jd_text": ""}
    job.update(kw)
    return job


def test_message_mentions_company_and_title():
    c = Composer(MINI_PROFILE, MINI_TEMPLATES, seed=1)
    msg = c.compose(_job())
    assert "某某科技" in msg.text or "嵌入式软件工程师" in msg.text


def test_family_is_picked_by_title():
    c = Composer(MINI_PROFILE, MINI_TEMPLATES, seed=1)
    assert c.pick_family("嵌入式软件工程师")["id"] == "embedded_sw"
    assert c.pick_family("随便一个岗位")["id"] == "general"


def test_project_picked_by_jd_tag_hits():
    c = Composer(MINI_PROFILE, MINI_TEMPLATES, seed=1)
    # JD 讲 ESP32 → 该挑 ESP32 那个项目，而不是 STM32
    p = c.pick_project("熟悉 esp32-idf 与 wifi 连接")
    assert p["id"] == "p2"


def test_tech_hooks_injected_when_jd_mentions_them():
    c = Composer(MINI_PROFILE, MINI_TEMPLATES, seed=1)
    hooks = c.pick_hooks("需要熟悉 FreeRTOS 和 CAN 总线")
    assert any("RTOS" in h for h in hooks)
    assert any("CAN" in h for h in hooks)


def test_ascii_tag_needs_word_boundary():
    c = Composer(MINI_PROFILE, MINI_TEMPLATES, seed=1)
    # "C" 不能命中 "PCB"
    assert c.pick_skills("画过 PCB 板") != ["C 语言"]
    # 单独出现的 C 要能命中
    assert "C 语言" in c.pick_skills("用 C 写固件")


def test_message_respects_max_chars():
    tpl = dict(MINI_TEMPLATES)
    tpl["settings"] = dict(MINI_TEMPLATES["settings"], max_chars=60)
    c = Composer(MINI_PROFILE, tpl, seed=1)
    msg = c.compose(_job())
    assert len(msg.text) <= 60


def test_no_leftover_placeholders():
    c = Composer(MINI_PROFILE, MINI_TEMPLATES, seed=1)
    msg = c.compose(_job())
    assert not re.search(r"\{[a-z_]+\}", msg.text)


def test_same_seed_is_reproducible():
    a = Composer(MINI_PROFILE, MINI_TEMPLATES, seed=42).compose(_job())
    b = Composer(MINI_PROFILE, MINI_TEMPLATES, seed=42).compose(_job())
    assert a.text == b.text


def test_validate_text_flags_missing_company_and_title():
    warns = validate_text("您好，我想投递。", {"company": "星河", "title": "嵌入式"},
                          {"max_chars": 300, "min_chars": 5})
    assert any("群发" in w for w in warns)


def test_validate_text_flags_braces():
    warns = validate_text("您好 {company}", {"company": "星河", "title": "嵌入式"},
                          {"max_chars": 300, "min_chars": 5})
    assert any("花括号" in w for w in warns)


def test_validate_text_passes_good_message():
    warns = validate_text("您好，看到星河在招嵌入式。",
                          {"company": "星河", "title": "嵌入式"},
                          {"max_chars": 300, "min_chars": 5})
    assert warns == []
