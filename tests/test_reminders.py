"""定时提醒（M5）的用例：创建、参数、平台门控、取消联动。"""

from __future__ import annotations

from conftest import FakeCron, at, make_harness

REMIND_AT = at(1, "20:00")


def test_one_shot_reminder_job_parameters():
    h = make_harness()
    h.import_now(h.event(), [{"title": "喝水", "remind_at": REMIND_AT}], plan_name="生活")

    assert len(h.cron.jobs) == 1
    job = h.cron.jobs[0]
    assert job["run_once"] is True
    assert job["cron_expression"] is None
    assert job["timezone"] == "Asia/Shanghai"
    # 传 naive 本地时间，由 AstrBot 按 timezone 解释
    assert job["run_at"].isoformat() == f"{REMIND_AT}:00"
    assert job["run_at"].tzinfo is None


def test_reminder_payload_contract():
    h = make_harness()
    h.import_now(h.event(sender="10001"), [{"title": "喝水", "remind_at": REMIND_AT}], plan_name="生活")
    payload = h.cron.jobs[0]["payload"]
    assert payload["session"] == "aiocqhttp:private:10001"
    assert payload["sender_id"] == "10001"
    assert payload["origin"] == "plugin"  # 不用 "api"，避免强制 admin
    assert "喝水" in payload["note"]


def test_repeating_reminder_uses_cron_expression():
    h = make_harness()
    h.import_now(
        h.event(),
        [{"title": "记账", "remind_at": REMIND_AT, "remind_repeat": "daily"}],
        plan_name="生活",
    )
    job = h.cron.jobs[0]
    minute, hour = int(REMIND_AT[14:16]), int(REMIND_AT[11:13])
    assert job["cron_expression"] == f"{minute} {hour} * * *"
    assert job["run_once"] is False


def test_reminder_mapping_stored():
    h = make_harness()
    h.import_now(h.event(), [{"title": "喝水", "remind_at": REMIND_AT}], plan_name="生活")
    reminders = h.store["reminders::aiocqhttp:10001"]
    assert len(reminders) == 1
    entry = next(iter(reminders.values()))
    assert entry["job_id"] == "job1" and entry["list_name"] == "生活" and entry["repeat"] == "once"


def test_no_reminder_when_task_has_none():
    h = make_harness()
    h.import_now(h.event(), [{"title": "没有提醒"}], plan_name="生活")
    assert h.cron.jobs == [] and "reminders::aiocqhttp:10001" not in h.store


def test_webchat_platform_skips_cron_with_explanation():
    h = make_harness()
    out = h.import_now(
        h.event(platform="webchat", sender="astrbot"),
        [{"title": "喝水", "remind_at": REMIND_AT}],
        plan_name="生活",
    )
    assert h.cron.jobs == []
    assert "不支持机器人主动发消息" in out
    assert len(h.graph.tasks["L1"]) == 1  # To Do 内的任务仍然写入


def test_reminder_disabled_by_config():
    h = make_harness(config={"reminder_enabled": False})
    out = h.import_now(h.event(), [{"title": "喝水", "remind_at": REMIND_AT}], plan_name="生活")
    assert h.cron.jobs == [] and "关闭了「定时提醒」" in out


def test_cron_unavailable_degrades_gracefully():
    h = make_harness(with_cron=False)
    out = h.import_now(h.event(), [{"title": "喝水", "remind_at": REMIND_AT}], plan_name="生活")
    assert "定时任务不可用" in out
    assert len(h.graph.tasks["L1"]) == 1  # 导入本身仍然成功


def test_cron_failure_does_not_break_import():
    cron = FakeCron()
    cron.raise_on_add = RuntimeError("boom")
    h = make_harness(cron=cron)
    out = h.import_now(h.event(), [{"title": "喝水", "remind_at": REMIND_AT}], plan_name="生活")
    assert "成功 1 项" in out and "提醒没建上" in out


def test_completing_task_cancels_its_reminder():
    h = make_harness()
    h.import_now(h.event(), [{"title": "喝水", "remind_at": REMIND_AT}], plan_name="生活")
    out = h.complete_task(h.event(), {"query": "喝水"})
    assert "提醒已一并取消" in out
    assert h.cron.deleted == ["job1"]
    assert h.store.get("reminders::aiocqhttp:10001") == {}


def test_deleting_task_cancels_its_reminder():
    h = make_harness()
    event = h.event()
    h.import_now(event, [{"title": "喝水", "remind_at": REMIND_AT}], plan_name="生活")
    h.delete_task(event, {"query": "喝水"})
    h.delete_task(event, {"confirmed": True})
    assert h.cron.deleted == ["job1"]


def test_deleting_list_cancels_all_its_reminders():
    h = make_harness()
    event = h.event()
    h.import_now(
        event,
        [{"title": "喝水", "remind_at": REMIND_AT}, {"title": "记账", "remind_at": at(1, "21:00")}],
        plan_name="生活",
    )
    assert len(h.cron.jobs) == 2
    h.delete_list(event, {"list_name": "生活"})
    h.delete_list(event, {"confirmed": True})
    assert sorted(h.cron.deleted) == ["job1", "job2"]
    assert h.store.get("reminders::aiocqhttp:10001") == {}


def test_reopen_does_not_recreate_reminder():
    """取消完成不会凭空重建提醒——如实反映当前能力。"""
    h = make_harness()
    h.import_now(h.event(), [{"title": "喝水", "remind_at": REMIND_AT}], plan_name="生活")
    h.complete_task(h.event(), {"query": "喝水"})
    h.complete_task(h.event(), {"query": "喝水", "completed": False})
    assert len(h.cron.jobs) == 1  # 没有新增
