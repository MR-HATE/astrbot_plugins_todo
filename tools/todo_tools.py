"""LLM 工具的 Schema 定义。

每个 ``Todo*Tool`` 是一个 ``FunctionTool`` 子类：只负责声明 ``name`` / ``description`` /
``parameters`` 并把调用转发给插件实例（``self.plugin.tool_*``），
业务逻辑统一放在 ``main.py``，保证指令与工具行为一致。

命名统一加 ``ms_todo_`` 前缀，避免与 AstrBot 内置工具（如 proactive agent 的
``future_task``）或其它插件的工具重名。
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..graph.models import (
    CHECKLIST_PARAMETERS,
    COMPLETE_TASK_PARAMETERS,
    CONFIRM_PARAMETERS,
    DELETE_LIST_PARAMETERS,
    DELETE_TASK_PARAMETERS,
    IMPORT_PARAMETERS,
    LIST_TASKS_PARAMETERS,
    UPDATE_TASK_PARAMETERS,
)

_NO_PARAMS: dict = {"type": "object", "properties": {}}


@dataclass
class TodoToolBase(FunctionTool[AstrAgentContext]):
    """所有待办工具的基类。

    ``plugin`` 由插件在注册时注入（``ToolClass(plugin=self)``），
    工具通过它访问授权管理器与 Graph 客户端。
    """

    plugin: Any = None

    def _event(self, context: ContextWrapper[AstrAgentContext]):
        return context.context.event


@dataclass
class TodoAccountStatusTool(TodoToolBase):
    name: str = "ms_todo_account_status"
    description: str = (
        "查询当前用户是否已绑定 Microsoft To Do 账号，以及绑定的账号、token 有效期。"
        "当用户询问待办相关能力、或需要判断是否要先授权时调用。"
    )
    parameters: dict = Field(default_factory=lambda: dict(_NO_PARAMS))

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_account_status(self._event(context))


@dataclass
class TodoLoginStartTool(TodoToolBase):
    name: str = "ms_todo_login_start"
    description: str = (
        "为当前用户发起 Microsoft To Do 账号绑定（OAuth 2.0 设备码授权）。"
        "返回一段包含授权网址和设备码的说明，必须把网址和代码原样转达给用户。"
        "当用户未绑定账号、或主动要求绑定/重新授权 Microsoft 待办时调用。"
    )
    parameters: dict = Field(default_factory=lambda: dict(_NO_PARAMS))

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_login_start(self._event(context))


@dataclass
class TodoLogoutTool(TodoToolBase):
    name: str = "ms_todo_logout"
    description: str = (
        "解除当前用户与本机器人的 Microsoft To Do 绑定（删除本地保存的凭据）。"
        "仅在用户明确要求解绑/取消授权时调用。"
    )
    parameters: dict = Field(default_factory=lambda: dict(_NO_PARAMS))

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_logout(self._event(context))


@dataclass
class TodoListTaskListsTool(TodoToolBase):
    name: str = "ms_todo_list_task_lists"
    description: str = (
        "列出当前用户 Microsoft To Do 中已有的待办列表（清单）。"
        "可用于确认连接是否正常，或在导入任务前让用户选择目标列表。"
    )
    parameters: dict = Field(default_factory=lambda: dict(_NO_PARAMS))

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_list_task_lists(self._event(context))


@dataclass
class TodoImportTasksTool(TodoToolBase):
    name: str = "ms_todo_import_tasks"
    description: str = (
        "把整理好的待办清单导入 Microsoft To Do（两阶段提交）。\n"
        "用法：先把用户说的计划整理成 tasks 数组并调用本工具（confirmed 留空/false），"
        "工具会返回一份「预览」和 plan_id；把预览原样展示给用户并等待确认；"
        "用户确认后再调用一次（confirmed=true，带上 plan_id）才会真正写入。\n"
        "目标列表按 plan_name（计划名，如「上海出差」）自动创建或复用；"
        "用户明确指定列表名时用 list_name。"
    )
    parameters: dict = Field(default_factory=lambda: copy.deepcopy(IMPORT_PARAMETERS))

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_import_tasks(self._event(context), kwargs)


@dataclass
class TodoConfirmImportTool(TodoToolBase):
    name: str = "ms_todo_confirm_import"
    description: str = (
        "确认或取消上一次暂存的待办清单。"
        "用户说「确认/可以/导入吧」时用 action=confirm；"
        "用户说「算了/取消/不要了」时用 action=cancel。"
        "仅在已经出现过清单预览之后调用。"
    )
    parameters: dict = Field(default_factory=lambda: copy.deepcopy(CONFIRM_PARAMETERS))

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_confirm_import(self._event(context), kwargs)


@dataclass
class TodoListTasksTool(TodoToolBase):
    name: str = "ms_todo_list_tasks"
    description: str = (
        "读取用户 Microsoft To Do 中的待办，**包含完成状态**（未完成/进行中/已完成/已逾期）。\n"
        "用于回答「今天还有什么要做的」「上周的事做完了吗」「XX 列表里还有啥」这类问题。\n"
        "默认 scope=pending（未完成且今天到期或已逾期）；也可用 "
        "today / overdue / upcoming / completed / all；list_name 留空则汇总所有列表。\n"
        "注意：这是只读操作，不会修改任何待办。"
    )
    parameters: dict = Field(default_factory=lambda: copy.deepcopy(LIST_TASKS_PARAMETERS))

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_list_tasks(self._event(context), kwargs)


@dataclass
class TodoUpdateTaskTool(TodoToolBase):
    name: str = "ms_todo_update_task"
    description: str = (
        "修改一条已有待办：改标题、改截止时间、改优先级、改状态、改备注，或清除截止时间。\n"
        "先用 query（标题关键词）或 task_id 定位；如果匹配到多条，工具会返回候选清单，"
        "这时要让用户确认是哪一个，再用 id 或更精确的标题重新调用。\n"
        "用户说「把交房租改到周五」「这件事不急」时用它。"
    )
    parameters: dict = Field(default_factory=lambda: copy.deepcopy(UPDATE_TASK_PARAMETERS))

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_update_task(self._event(context), kwargs)


@dataclass
class TodoCompleteTaskTool(TodoToolBase):
    name: str = "ms_todo_complete_task"
    description: str = (
        "把一条待办标记为已完成（或取消完成）。\n"
        "用户说「交房租做完了」「这个我搞定了」时用 completed=true（默认）；"
        "说「那个还没做，帮我改回来」时用 completed=false。\n"
        "定位方式同其它工具：query（标题关键词）或 task_id。"
    )
    parameters: dict = Field(default_factory=lambda: copy.deepcopy(COMPLETE_TASK_PARAMETERS))

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_complete_task(self._event(context), kwargs)


@dataclass
class TodoDeleteTaskTool(TodoToolBase):
    name: str = "ms_todo_delete_task"
    description: str = (
        "删除一条待办（**不可恢复**，两阶段确认）。\n"
        "第一次调用（confirmed 留空/false）只会返回「将删除哪些」的清单；"
        "把清单给用户看，用户确认后再用 confirmed=true 调用一次才真正删除。\n"
        "用户说「删掉买菜这条」「这条不用了」时用它。不要用它来标记完成——那是 ms_todo_complete_task。"
    )
    parameters: dict = Field(default_factory=lambda: copy.deepcopy(DELETE_TASK_PARAMETERS))

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_delete_task(self._event(context), kwargs)


@dataclass
class TodoAddChecklistTool(TodoToolBase):
    name: str = "ms_todo_add_checklist_items"
    description: str = (
        "给一条已有待办追加子步骤（To Do 里的「步骤」）。\n"
        "用户说「搬家那件事下面加上打包、找搬家公司」时用它，"
        "先用 query 或 task_id 定位任务，items 是要追加的步骤文本。"
    )
    parameters: dict = Field(default_factory=lambda: copy.deepcopy(CHECKLIST_PARAMETERS))

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_add_checklist_items(self._event(context), kwargs)


@dataclass
class TodoDeleteListTool(TodoToolBase):
    name: str = "ms_todo_delete_list"
    description: str = (
        "删除**整个待办列表**（连同列表里的所有任务），不可恢复，两阶段确认。\n"
        "第一次调用（confirmed 留空/false）只返回「将删除哪个列表、里面有多少任务」；"
        "把这段给用户看，用户确认后再用 confirmed=true 调用一次才真正删除。\n"
        "用户说「把上海出差那个列表删掉」时用它。"
        "注意：Microsoft To Do 的默认列表不能删除；只删单条任务请用 ms_todo_delete_task。"
    )
    parameters: dict = Field(default_factory=lambda: copy.deepcopy(DELETE_LIST_PARAMETERS))

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_delete_list(self._event(context), kwargs)


#: 注册顺序即工具在 WebUI「函数工具管理」中的展示顺序。
TOOL_CLASSES: tuple[type[TodoToolBase], ...] = (
    TodoAccountStatusTool,
    TodoLoginStartTool,
    TodoLogoutTool,
    TodoListTaskListsTool,
    TodoListTasksTool,
    TodoImportTasksTool,
    TodoConfirmImportTool,
    TodoUpdateTaskTool,
    TodoCompleteTaskTool,
    TodoDeleteTaskTool,
    TodoAddChecklistTool,
    TodoDeleteListTool,
)
