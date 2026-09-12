"""待办数据模型与函数调用 Schema。

这一层只描述「一条待办长什么样」以及「怎么变成 Graph 能吃的 JSON」，
不做时间解析、不做去重、不碰网络——那些在 ``planner`` 和 ``client`` 里。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

#: Graph todoTask 标题长度上限（官方未明示，To Do 客户端为 255，这里留点余量）
TITLE_MAX = 250
STEP_MAX = 250
NOTE_MAX = 4000
STEP_COUNT_MAX = 20

VALID_IMPORTANCE = ("low", "normal", "high")

#: 中文/口语 -> Graph importance 枚举
IMPORTANCE_ALIASES = {
    "low": "low",
    "低": "low",
    "不重要": "low",
    "normal": "normal",
    "普通": "normal",
    "中": "normal",
    "一般": "normal",
    "high": "high",
    "高": "high",
    "重要": "high",
    "紧急": "high",
    "很急": "high",
    "urgent": "high",
    "important": "high",
}

_WS_RE = re.compile(r"\s+")


def clean_text(value: Any, limit: int) -> str:
    """压缩空白并截断，避免把换行/超长内容直接塞给 Graph。"""
    if value is None:
        return ""
    text = _WS_RE.sub(" ", str(value)).strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


@dataclass
class TaskDraft:
    """一条待办（归一化之后的样子）。"""

    title: str
    due_date: str | None = None
    """YYYY-MM-DD（已在配置时区下解析为绝对日期）"""
    due_time: str | None = None
    """HH:MM，缺省表示只设日期不设具体时刻"""
    importance: str = "normal"
    note: str | None = None
    remind_at: str | None = None
    """YYYY-MM-DDTHH:MM，本地时间（配合配置时区）"""
    remind_repeat: str | None = None
    """提醒是否重复：``daily`` / ``weekly`` / ``monthly``；None 表示只提醒一次"""
    steps: list[str] = field(default_factory=list)
    source_key: str | None = None
    warnings: list[str] = field(default_factory=list)
    """归一化过程中被修正/丢弃的字段说明，会显示在预览里提醒用户"""

    # ---------------------------------------------------------------- 工具

    def fingerprint(self, list_name: str) -> str:
        """幂等指纹：同一列表 + 同一天 + 同标题视为同一条待办。"""
        raw = "|".join(
            (
                (list_name or "").strip().lower(),
                self.due_date or "",
                _WS_RE.sub("", self.title).lower(),
            )
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def to_graph_task(self, tz: str) -> dict:
        """转成 ``POST /me/todo/lists/{id}/tasks`` 的请求体。"""
        payload: dict[str, Any] = {
            "title": self.title,
            "importance": self.importance,
        }
        if self.note:
            payload["body"] = {"content": self.note, "contentType": "text"}
        if self.due_date:
            payload["dueDateTime"] = {
                "dateTime": f"{self.due_date}T{self.due_time or '00:00'}:00",
                "timeZone": tz,
            }
        if self.remind_at:
            payload["isReminderOn"] = True
            payload["reminderDateTime"] = {
                "dateTime": f"{self.remind_at}:00",
                "timeZone": tz,
            }
        return payload

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "due_date": self.due_date,
            "due_time": self.due_time,
            "importance": self.importance,
            "note": self.note,
            "remind_at": self.remind_at,
            "remind_repeat": self.remind_repeat,
            "steps": list(self.steps),
            "source_key": self.source_key,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskDraft":
        return cls(
            title=clean_text(data.get("title"), TITLE_MAX),
            due_date=data.get("due_date") or None,
            due_time=data.get("due_time") or None,
            importance=str(data.get("importance") or "normal"),
            note=data.get("note") or None,
            remind_at=data.get("remind_at") or None,
            remind_repeat=data.get("remind_repeat") or None,
            steps=[clean_text(s, STEP_MAX) for s in (data.get("steps") or []) if s],
            source_key=data.get("source_key") or None,
        )


@dataclass
class PlanDraft:
    """一次导入的暂存计划（两阶段提交的第一阶段产物）。"""

    plan_id: str
    aid: str
    session: str
    list_name: str
    tasks: list[TaskDraft] = field(default_factory=list)
    created_at: float = 0.0
    sender_id: str = ""
    """发起导入的用户 ID（定时任务用它判断角色，也方便以后区分多用户）"""

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "aid": self.aid,
            "session": self.session,
            "list_name": self.list_name,
            "created_at": self.created_at,
            "sender_id": self.sender_id,
            "tasks": [t.to_dict() for t in self.tasks],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlanDraft":
        return cls(
            plan_id=str(data.get("plan_id") or ""),
            aid=str(data.get("aid") or ""),
            session=str(data.get("session") or ""),
            list_name=str(data.get("list_name") or ""),
            created_at=float(data.get("created_at") or 0.0),
            sender_id=str(data.get("sender_id") or ""),
            tasks=[TaskDraft.from_dict(t) for t in (data.get("tasks") or []) if isinstance(t, dict)],
        )


# ---------------------------------------------------------------- 函数调用 Schema

TASK_ITEM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "待办标题，简洁的动作描述，例如「订好上海的酒店」。必填，≤250 字。",
        },
        "due_date": {
            "type": "string",
            "description": (
                "截止日期，绝对日期格式 YYYY-MM-DD（请先根据今天的日期把「明天」「下周三」换算好）。"
                "没有明确时间就不要填。"
            ),
        },
        "due_time": {
            "type": "string",
            "description": "截止时刻 HH:MM（24 小时制）。只有日期没有具体时刻时留空。",
        },
        "importance": {
            "type": "string",
            "enum": list(VALID_IMPORTANCE),
            "description": "优先级：low / normal / high。用户说「重要」「紧急」用 high。默认 normal。",
        },
        "note": {
            "type": "string",
            "description": "补充说明、地点、金额、联系人等，可留空。",
        },
        "remind_at": {
            "type": "string",
            "description": "提醒时间，格式 YYYY-MM-DDTHH:MM（本地时间）。用户明确要求提醒时才填。",
        },
        "remind_repeat": {
            "type": "string",
            "enum": ["none", "daily", "weekly", "monthly"],
            "description": (
                "提醒是否重复：用户说「每天」「每周五」「每月10号」时分别填 "
                "daily / weekly / monthly，并让 remind_at 落在最近一次的对应时刻；"
                "只说一次性提醒就填 none 或留空。"
            ),
        },
        "steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "该待办的子步骤（会写入 To Do 的「步骤/checklist」），没有就留空数组。",
        },
        "source_key": {
            "type": "string",
            "description": "幂等标识，可留空（留空时由插件按 列表+日期+标题 自动生成）。",
        },
    },
    "required": ["title"],
}

IMPORT_PARAMETERS: dict = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": TASK_ITEM_SCHEMA,
            "description": "要导入的待办列表。一次导入只针对一个目标列表。",
        },
        "plan_name": {
            "type": "string",
            "description": (
                "这次计划的名称，例如「上海出差」「搬家」。用作目标列表名，"
                "用户给了标题就用用户的标题。"
            ),
        },
        "list_name": {
            "type": "string",
            "description": "直接指定目标列表名（优先级高于 plan_name）。用户明确说「记到 XX 列表」时填写。",
        },
        "confirmed": {
            "type": "boolean",
            "description": (
                "是否已获得用户确认。**第一次调用必须留空或 false**（只是生成预览），"
                "等用户确认后再用 confirmed=true 调用一次。"
            ),
        },
        "plan_id": {
            "type": "string",
            "description": "确认已有预览时，回传预览里给出的 plan_id。",
        },
    },
    "required": ["tasks"],
}

CONFIRM_PARAMETERS: dict = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["confirm", "cancel"],
            "description": "confirm=把暂存的清单写入 Microsoft To Do；cancel=丢弃这次暂存。",
        },
        "plan_id": {
            "type": "string",
            "description": "预览里给出的 plan_id；留空则使用当前用户最近一次暂存的计划。",
        },
    },
    "required": ["action"],
}

LIST_TASKS_PARAMETERS: dict = {
    "type": "object",
    "properties": {
        "scope": {
            "type": "string",
            "enum": ["pending", "today", "overdue", "upcoming", "completed", "all"],
            "description": (
                "要看的范围。pending（默认）= 未完成且截止日期是今天或已逾期，"
                "回答「今天还有什么要做的」用它；today=仅今天到期；overdue=已逾期；"
                "upcoming=未来 7 天内到期；completed=已完成的；all=全部。"
            ),
        },
        "list_name": {
            "type": "string",
            "description": "只看某个列表；留空则汇总用户所有列表。",
        },
        "limit": {
            "type": "integer",
            "description": "最多返回多少条，默认 30。",
        },
    },
    "required": [],
}

#: 定位一条任务的公共参数（增删改类工具共用）
_TASK_LOCATOR_PROPS: dict = {
    "query": {
        "type": "string",
        "description": (
            "用于定位任务的标题或关键词（模糊匹配，忽略大小写）。"
            "例如用户说「把交房租改到周五」，query 就是「交房租」。"
        ),
    },
    "task_id": {
        "type": "string",
        "description": "任务 ID。之前查询结果里给过 id 时可以直接用，比标题更精确。",
    },
    "list_name": {
        "type": "string",
        "description": "限定在某个列表里查找；留空则搜索用户所有列表。",
    },
}

UPDATE_TASK_PARAMETERS: dict = {
    "type": "object",
    "properties": {
        **_TASK_LOCATOR_PROPS,
        "title": {"type": "string", "description": "新的标题。不改标题就留空。"},
        "due_date": {
            "type": "string",
            "description": "新的截止日期，YYYY-MM-DD（相对说法如「周五」也可以）。不改就留空。",
        },
        "due_time": {"type": "string", "description": "新的截止时刻 HH:MM。不改就留空。"},
        "importance": {
            "type": "string",
            "enum": list(VALID_IMPORTANCE),
            "description": "新的优先级。不改就留空。",
        },
        "status": {
            "type": "string",
            "enum": ["notStarted", "inProgress", "completed", "waitingOnOthers", "deferred"],
            "description": "新的状态。标记完成请优先用 ms_todo_complete_task。不改就留空。",
        },
        "note": {"type": "string", "description": "新的备注（会覆盖原备注）。"},
        "clear_due": {
            "type": "boolean",
            "description": "设为 true 清除截止日期（用户说「不用设截止时间了」时）。",
        },
    },
    "required": [],
}

COMPLETE_TASK_PARAMETERS: dict = {
    "type": "object",
    "properties": {
        **_TASK_LOCATOR_PROPS,
        "completed": {
            "type": "boolean",
            "description": "true（默认）= 标记完成；false = 取消完成（重新打开）。",
        },
    },
    "required": [],
}

DELETE_TASK_PARAMETERS: dict = {
    "type": "object",
    "properties": {
        **_TASK_LOCATOR_PROPS,
        "confirmed": {
            "type": "boolean",
            "description": (
                "**第一次调用必须留空或 false**：工具会返回「将删除哪些」的清单等用户确认；"
                "用户确认后再用 confirmed=true 调用一次才真正删除。删除不可恢复。"
            ),
        },
    },
    "required": [],
}

CHECKLIST_PARAMETERS: dict = {
    "type": "object",
    "properties": {
        **_TASK_LOCATOR_PROPS,
        "items": {
            "type": "array",
            "items": {"type": "string"},
            "description": "要追加到该任务的子步骤（checklist）文本列表。",
        },
    },
    "required": ["items"],
}

DELETE_LIST_PARAMETERS: dict = {
    "type": "object",
    "properties": {
        "list_name": {
            "type": "string",
            "description": "要删除的列表名（必填）。例如用户说「把上海出差那个列表删掉」，就填「上海出差」。",
        },
        "confirmed": {
            "type": "boolean",
            "description": (
                "**第一次调用必须留空或 false**：工具会返回「将删除哪个列表、里面有多少任务」；"
                "把这段给用户看，用户确认后再用 confirmed=true 调用一次才真正删除。"
                "整个列表和里面的任务都会消失，不可恢复。"
            ),
        },
    },
    "required": ["list_name"],
}
