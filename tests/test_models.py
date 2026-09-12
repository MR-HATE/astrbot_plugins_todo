"""``graph/models.py`` 与工具 Schema 的用例。"""

from __future__ import annotations

from conftest import days
from plugin_pkg.graph.models import PlanDraft, TaskDraft
from plugin_pkg.tools import TOOL_CLASSES


def test_to_graph_task_basic_payload():
    task = TaskDraft(
        title="订酒店",
        due_date="2026-09-16",
        due_time="18:00",
        importance="high",
        note="预算 500",
    )
    payload = task.to_graph_task("Asia/Shanghai")
    assert payload["title"] == "订酒店"
    assert payload["importance"] == "high"
    assert payload["body"] == {"content": "预算 500", "contentType": "text"}
    assert payload["dueDateTime"] == {
        "dateTime": "2026-09-16T18:00:00",
        "timeZone": "Asia/Shanghai",
    }
    assert "reminderDateTime" not in payload


def test_to_graph_task_reminder_requires_flag():
    task = TaskDraft(title="x", remind_at="2026-09-16T20:00")
    payload = task.to_graph_task("Asia/Shanghai")
    # Graph 要求 isReminderOn=true 才认 reminderDateTime
    assert payload["isReminderOn"] is True
    assert payload["reminderDateTime"]["dateTime"] == "2026-09-16T20:00:00"


def test_to_graph_task_date_only_defaults_midnight():
    payload = TaskDraft(title="x", due_date="2026-09-16").to_graph_task("Asia/Shanghai")
    assert payload["dueDateTime"]["dateTime"] == "2026-09-16T00:00:00"


def test_task_draft_round_trip_keeps_repeat():
    task = TaskDraft(title="记账", remind_at=days(1) + "T21:00", remind_repeat="daily", steps=["a"])
    restored = TaskDraft.from_dict(task.to_dict())
    assert restored.title == task.title
    assert restored.remind_at == task.remind_at
    assert restored.remind_repeat == "daily"
    assert restored.steps == ["a"]


def test_plan_draft_round_trip_keeps_sender():
    plan = PlanDraft(
        plan_id="p1", aid="aiocqhttp:1", session="yume:FriendMessage:1",
        sender_id="1", list_name="生活", tasks=[TaskDraft(title="x")], created_at=1.0,
    )
    restored = PlanDraft.from_dict(plan.to_dict())
    assert restored.sender_id == "1"
    assert restored.tasks[0].title == "x"
    assert restored.list_name == "生活"


def test_all_tool_schemas_are_valid():
    """实例化每个工具类会触发 pydantic 对 parameters 的 JSON Schema 校验。"""
    names = set()
    for tool_cls in TOOL_CLASSES:
        tool = tool_cls(plugin=None)
        assert tool.name and tool.description
        assert tool.parameters.get("type") == "object"
        assert "properties" in tool.parameters
        names.add(tool.name)
    assert len(names) == len(TOOL_CLASSES), "工具名重复"
    assert "ms_todo_import_tasks" in names and "ms_todo_delete_list" in names


def test_import_schema_exposes_repeat_enum():
    from astrbot_plugins_todo.graph.models import TASK_ITEM_SCHEMA

    props = TASK_ITEM_SCHEMA["properties"]
    assert "remind_at" in props and "remind_repeat" in props
    assert set(props["remind_repeat"]["enum"]) == {"none", "daily", "weekly", "monthly"}
    assert TASK_ITEM_SCHEMA["required"] == ["title"]
