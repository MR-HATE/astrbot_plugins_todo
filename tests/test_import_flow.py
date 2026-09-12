"""导入链路（两阶段提交）的用例。"""

from __future__ import annotations

import time

from conftest import days, make_harness

TASKS = [
    {"title": "订酒店", "due_date": days(2), "steps": ["比价", "下单"]},
    {"title": "订机票", "due_date": days(2)},
    {"title": "收拾行李", "due_date": days(1)},
]


def test_preview_writes_nothing():
    h = make_harness()
    out = h.stage(h.event(), {"tasks": TASKS, "plan_name": "出差"})
    assert "待办预览" in out and "plan_id" in out
    assert h.graph.tasks.get("L1") == []  # 未确认前不落库
    assert not [c for c in h.graph.calls if c[0] in ("create_task", "batch")]


def test_confirm_writes_all_and_creates_list():
    h = make_harness()
    out = h.import_now(h.event(), TASKS, plan_name="出差")
    assert "成功 3 项" in out
    assert "出差" in h.graph.lists
    assert len(h.graph.tasks["L2"]) == 3
    assert len([c for c in h.graph.calls if c[0] == "batch"]) == 1  # 多条走批量
    assert len([c for c in h.graph.calls if c[0] == "checklist"]) == 2


def test_single_task_uses_direct_create():
    h = make_harness()
    h.import_now(h.event(), [{"title": "只有一条"}])
    kinds = [c[0] for c in h.graph.calls]
    assert "create_task" in kinds and "batch" not in kinds


def test_cross_machine_dedup_after_kv_cleared():
    h = make_harness()
    event = h.event()
    h.import_now(event, TASKS, plan_name="出差")
    assert len(h.graph.tasks["L2"]) == 3

    h.store.clear()  # 模拟换机器 / 重装，本地幂等表没了
    out = h.import_now(event, TASKS, plan_name="出差")
    assert "没有新增待办" in out and "跳过 3 项" in out
    assert len(h.graph.tasks["L2"]) == 3  # 没有产生重复


def test_dedup_detects_manually_created_task():
    h = make_harness()
    h.graph.add_task("L1", "买菜", dueDateTime={"dateTime": f"{days(0)}T00:00:00.0000000"})
    out = h.import_now(h.event(), [{"title": "买菜", "due_date": days(0)}], plan_name="生活")
    assert "跳过 1 项" in out


def test_require_confirm_false_writes_directly():
    h = make_harness(config={"require_confirm": False})
    event = h.event()
    out = h.stage(event, {"tasks": [{"title": "直接写入"}], "confirmed": True})
    assert "成功 1 项" in out
    assert h.pending_key(event) not in h.store


def test_confirm_without_staged_plan_is_rejected():
    h = make_harness()
    out = h.confirm_import(h.event(), {"action": "confirm"})
    assert "没有找到待确认的清单" in out


def test_plan_id_mismatch_is_rejected():
    h = make_harness()
    event = h.event()
    h.stage(event, {"tasks": TASKS})
    out = h.confirm_import(event, {"action": "confirm", "plan_id": "deadbeef"})
    assert "plan_id 不匹配" in out
    assert h.graph.tasks.get("L1") == []


def test_expired_plan_is_not_executed():
    h = make_harness()
    event = h.event()
    h.stage(event, {"tasks": TASKS})
    key = h.pending_key(event)
    h.store[key]["created_at"] = time.time() - 10_000  # 人为做旧
    out = h.confirm_import(event, {"action": "confirm", "plan_id": h.store[key]["plan_id"]})
    assert "超过" in out and "没有导入" in out
    assert h.graph.tasks.get("L1") == []


def test_cancel_drops_staged_plan():
    h = make_harness()
    event = h.event()
    h.stage(event, {"tasks": TASKS})
    out = h.confirm_import(event, {"action": "cancel"})
    assert "已放弃" in out
    assert h.pending_key(event) not in h.store


def test_max_tasks_per_import_truncates_with_note():
    h = make_harness(config={"max_tasks_per_import": 2})
    out = h.stage(h.event(), {"tasks": [{"title": f"任务{i}"} for i in range(5)]})
    assert "只包含前 2 项" in out


def test_unbound_account_is_blocked_early():
    h = make_harness(bound=False)
    out = h.stage(h.event(), {"tasks": TASKS})
    assert "还没有绑定" in out
    assert not h.store


def test_allow_list_blocks_other_users():
    h = make_harness(config={"allow_any_user": False, "allowed_users": ["999"]})
    out = h.stage(h.event(sender="10001"), {"tasks": TASKS})
    assert "白名单" in out


def test_auto_create_list_off_falls_back_to_default():
    h = make_harness(config={"auto_create_list": False, "default_list": "我的待办"})
    out = h.import_now(h.event(), TASKS, plan_name="出差")
    assert "成功 3 项" in out
    assert "我的待办" in h.graph.lists and "出差" not in h.graph.lists


def test_default_list_still_created_when_auto_create_off():
    """默认列表无论如何都要保证存在，否则会无从写入。"""
    h = make_harness(config={"auto_create_list": False})
    h.graph.lists.clear()  # 一个列表都没有
    out = h.import_now(h.event(), [{"title": "x"}], plan_name="随便")
    assert "成功 1 项" in out


def test_import_reports_partial_failure():
    h = make_harness()
    h.graph.fail_titles.add("订机票")
    out = h.import_now(h.event(), TASKS, plan_name="出差")
    assert "成功 2 项" in out and "失败 1 项" in out and "订机票" in out
