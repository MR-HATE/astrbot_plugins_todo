"""增删改查（4.2 / 4.7）的用例。"""

from __future__ import annotations

from conftest import days, make_harness


def _harness_with_tasks():
    h = make_harness()
    h.graph.add_task("L1", "交房租", dueDateTime={"dateTime": f"{days(0)}T18:00:00.0000000"})
    h.graph.add_task("L1", "写周报")
    h.graph.add_task("L1", "写季度报告")
    return h


# ---------------------------------------------------------------- 修改


def test_update_due_keeps_existing_time():
    h = _harness_with_tasks()
    out = h.update_task(h.event(), {"query": "交房租", "due_date": days(5)})
    assert "已修改" in out
    call = [c for c in h.graph.calls if c[0] == "update_task"][-1]
    assert call[2]["dueDateTime"]["dateTime"] == f"{days(5)}T18:00:00"  # 保留原时刻


def test_update_importance_alias_and_note():
    h = _harness_with_tasks()
    h.update_task(h.event(), {"task_id": "t1", "importance": "紧急", "note": "记得要发票"})
    payload = [c for c in h.graph.calls if c[0] == "update_task"][-1][2]
    assert payload["importance"] == "high"
    assert payload["body"]["content"] == "记得要发票"


def test_update_clear_due_sends_null():
    h = _harness_with_tasks()
    h.update_task(h.event(), {"task_id": "t1", "clear_due": True})
    payload = [c for c in h.graph.calls if c[0] == "update_task"][-1][2]
    assert payload["dueDateTime"] is None


def test_update_ambiguous_query_lists_candidates_without_patching():
    h = _harness_with_tasks()
    out = h.update_task(h.event(), {"query": "写", "importance": "high"})
    assert "找到 2 个匹配" in out and "id=" in out
    assert not [c for c in h.graph.calls if c[0] == "update_task"]


def test_update_exact_title_wins_over_partial():
    h = _harness_with_tasks()
    out = h.update_task(h.event(), {"query": "写周报", "title": "写周报（新）"})
    assert "已修改" in out


def test_update_unknown_task_is_reported():
    h = _harness_with_tasks()
    assert "没有找到匹配的待办" in h.update_task(h.event(), {"query": "不存在的事"})


def test_update_without_changes_is_reported():
    h = _harness_with_tasks()
    assert "没有识别出要修改的内容" in h.update_task(h.event(), {"task_id": "t1"})


# ---------------------------------------------------------------- 完成


def test_complete_and_reopen():
    h = _harness_with_tasks()
    assert "已完成" in h.complete_task(h.event(), {"query": "写周报"})
    assert h.graph.tasks["L1"][1]["status"] == "completed"
    assert "已重新打开" in h.complete_task(h.event(), {"query": "写周报", "completed": False})
    assert h.graph.tasks["L1"][1]["status"] == "notStarted"


# ---------------------------------------------------------------- 删除任务


def test_delete_task_requires_confirmation():
    h = _harness_with_tasks()
    event = h.event()
    out = h.delete_task(event, {"query": "交房租"})
    assert "即将删除 1 项" in out
    assert not [c for c in h.graph.calls if c[0] == "delete_task"]

    h.delete_task(event, {"query": "交房租"})  # 再问一次仍不删
    assert not [c for c in h.graph.calls if c[0] == "delete_task"]

    out = h.delete_task(event, {"confirmed": True})
    assert "已删除 1 项" in out
    assert [c for c in h.graph.calls if c[0] == "delete_task"] == [("delete_task", "t1")]


def test_delete_task_clears_seen_record():
    h = _harness_with_tasks()
    event = h.event()
    h.store["seen::L1"] = {"k": {"task_id": "t1", "title": "交房租"}}
    h.delete_task(event, {"query": "交房租"})
    h.delete_task(event, {"confirmed": True})
    assert "k" not in h.store.get("seen::L1", {})


def test_delete_without_pending_is_reported():
    h = _harness_with_tasks()
    assert "没有待确认的删除" in h.delete_task(h.event(), {"confirmed": True})


def test_delete_ambiguous_query_asks_user():
    h = _harness_with_tasks()
    out = h.delete_task(h.event(), {"query": "写"})
    assert "找到 2 个匹配" in out
    assert not [c for c in h.graph.calls if c[0] == "delete_task"]


# ---------------------------------------------------------------- 删除列表


def test_delete_list_two_phase_and_cleanup():
    h = _harness_with_tasks()
    event = h.event()
    h.store["seen::L1"] = {"k": {"task_id": "t1"}}
    out = h.delete_list(event, {"list_name": "生活"})
    assert "即将删除整个列表" in out and "3 个任务" in out
    assert not [c for c in h.graph.calls if c[0] == "delete_list"]

    out = h.delete_list(event, {"confirmed": True})
    assert "已删除列表" in out
    assert ("delete_list", "L1") in h.graph.calls
    assert "seen::L1" not in h.store


def test_delete_default_list_is_refused():
    h = make_harness()
    h.graph.add_list("Tasks", "LD", wellknownListName="defaultList")
    out = h.delete_list(h.event(), {"list_name": "Tasks"})
    assert "默认列表" in out
    assert not [c for c in h.graph.calls if c[0] == "delete_list"]


def test_delete_list_ambiguous_name():
    h = make_harness()
    h.graph.add_list("上海出差", "LA")
    h.graph.add_list("上海搬家", "LB")
    assert "找到多个相近的列表" in h.delete_list(h.event(), {"list_name": "上海"})


def test_delete_missing_list_is_reported():
    h = make_harness()
    assert "没找到名为" in h.delete_list(h.event(), {"list_name": "不存在"})


def test_delete_list_without_name_asks_for_one():
    h = make_harness()
    assert "请说明要删除哪个列表" in h.delete_list(h.event(), {})


# ---------------------------------------------------------------- 子步骤


def test_add_checklist_items_filters_empty():
    h = _harness_with_tasks()
    out = h.add_checklist(h.event(), {"query": "写周报", "items": ["a", "", "b"]})
    assert "添加 2 个子步骤" in out
    assert [c[2] for c in h.graph.calls if c[0] == "checklist"] == ["a", "b"]


def test_add_checklist_without_items_is_reported():
    h = _harness_with_tasks()
    assert "没有收到要添加的子步骤" in h.add_checklist(h.event(), {"query": "写周报", "items": []})


# ---------------------------------------------------------------- 查询


def test_list_tasks_scopes_and_list_name():
    h = _harness_with_tasks()
    h.graph.add_task("L1", "逾期的", dueDateTime={"dateTime": f"{days(-2)}T09:00:00.0000000"})
    out = h.list_tasks(h.event(), {"scope": "overdue"})
    assert "逾期的" in out and "交房租" not in out

    out = h.list_tasks(h.event(), {"scope": "all", "list_name": "生活"})
    assert "交房租" in out

    assert "没找到名为" in h.list_tasks(h.event(), {"scope": "all", "list_name": "没有这个列表"})
