"""Microsoft To Do（Graph API）接入层。

模块划分：
- ``errors``:  插件内统一的异常类型
- ``store``:   基于 AstrBot 插件 KV 的凭据存储
- ``auth``:    OAuth 2.0 Device Code Flow（含 refresh token 自动续期）
- ``client``:  Microsoft Graph REST 薄封装（重试 / 限流 / 错误分类）
- ``models``:  待办数据模型与函数调用 Schema
- ``planner``: 时间归一化 / 校验 / 去重 / 预览渲染
"""

from .auth import AccountStatus, DeviceCodeAuth, DeviceCodeChallenge
from .client import GraphClient
from .errors import (
    AuthFlowError,
    AuthRequiredError,
    ConfigError,
    GraphAPIError,
    TodoError,
)
from .models import (
    CONFIRM_PARAMETERS,
    IMPORT_PARAMETERS,
    LIST_TASKS_PARAMETERS,
    TASK_ITEM_SCHEMA,
    PlanDraft,
    TaskDraft,
)
from .planner import (
    PENDING_TTL,
    apply_seen,
    dedupe_batch,
    filter_tasks,
    local_now,
    local_today,
    normalize_task_status,
    normalize_tasks,
    plan_expired,
    render_import_result,
    render_preview,
    render_task_list,
    sort_tasks,
)
from .store import AccountStore

__all__ = [
    "CONFIRM_PARAMETERS",
    "IMPORT_PARAMETERS",
    "LIST_TASKS_PARAMETERS",
    "PENDING_TTL",
    "TASK_ITEM_SCHEMA",
    "AccountStatus",
    "AccountStore",
    "AuthFlowError",
    "AuthRequiredError",
    "ConfigError",
    "DeviceCodeAuth",
    "DeviceCodeChallenge",
    "GraphAPIError",
    "GraphClient",
    "PlanDraft",
    "TaskDraft",
    "TodoError",
    "apply_seen",
    "dedupe_batch",
    "filter_tasks",
    "local_now",
    "local_today",
    "normalize_task_status",
    "normalize_tasks",
    "plan_expired",
    "render_import_result",
    "render_preview",
    "render_task_list",
    "sort_tasks",
]
